# ===========================================================================
#  Submit the winget manifest to microsoft/winget-pkgs
# ===========================================================================
#  winget does not read manifests from our repo - the package only becomes
#  installable once a manifest set lives inside microsoft/winget-pkgs. That
#  means: fork, add the files, open a PR, get it reviewed.
#
#  Every new release needs a fresh PR (a new version folder), which is why
#  this is a script rather than a one-off.
#
#  Usage:
#      $env:GH_TOKEN = '<token with repo scope>'
#      .\_submit-winget.ps1                 # version read from live_notify.py
#      .\_submit-winget.ps1 -DryRun         # show what would happen
#
#  Prerequisites:
#      .\_package.ps1 -Exe <exe>     # builds the zip the manifest points at
#      .\_release.ps1 -Asset <zip>   # uploads it to the GitHub Release
#      winget validate --manifest winget   # must pass
#
#  KEEP THIS FILE PURE ASCII. See "PowerShell script encoding" in the docs:
#  PowerShell 5.1 decodes BOM-less scripts as ANSI, and an odd number of high
#  bytes on a line swallows the newline, turning the next line into a comment.
#  Chinese goes in winget/*.yaml instead - this script reads it from there.
# ===========================================================================

param(
    [string]$Version,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$upstream = 'microsoft/winget-pkgs'
$me = 'KagurazakaChizuru'
$fork = "$me/winget-pkgs"

# --- version ---------------------------------------------------------------
if (-not $Version) {
    $src = Get-Content (Join-Path $root 'live_notify.py') -Raw -Encoding UTF8
    $m = [regex]::Match($src, 'VERSION\s*=\s*"([^"]+)"')
    if (-not $m.Success) { throw 'VERSION not found in live_notify.py' }
    $Version = $m.Groups[1].Value
}
Write-Host "version: $Version"

# --- the manifest set we are shipping --------------------------------------
$srcDir = Join-Path $root 'winget'
if (-not (Test-Path $srcDir)) { throw "no winget folder at $srcDir" }

$identifier = 'KagurazakaChizuru.DafeiyuLiveNotify'
$publisher = 'KagurazakaChizuru'
$packageName = 'DafeiyuLiveNotify'
# winget-pkgs layout: manifests/<first letter lower>/<Publisher>/<Package>/<Version>/
$destDir = "manifests/$($identifier.Substring(0,1).ToLower())/$publisher/$packageName/$Version"

$files = Get-ChildItem $srcDir -File
if ($files.Count -eq 0) { throw "winget folder is empty" }
Write-Host "manifest: $identifier $Version  ($($files.Count) files -> $destDir)"

# Pull the Chinese short description out of the locale file so this script
# itself can stay ASCII.
$localeFile = $files | Where-Object { $_.Name -like '*.locale.*.yaml' } | Select-Object -First 1
$shortDesc = ''
if ($localeFile) {
    $t = Get-Content $localeFile.FullName -Raw -Encoding UTF8
    $m = [regex]::Match($t, '(?m)^ShortDescription:\s*(.+)$')
    if ($m.Success) { $shortDesc = $m.Groups[1].Value.Trim() }
}

if ($DryRun) {
    Write-Host ''
    Write-Host "fork    : $fork"
    Write-Host "branch  : dafeiyu-live-notify-$Version"
    Write-Host "dest    : $destDir"
    Write-Host "files   :"
    $files | ForEach-Object { Write-Host "    $($_.Name)" }
    Write-Host "desc    : $shortDesc"
    return
}

# --- auth ------------------------------------------------------------------
$token = $env:GH_TOKEN
if (-not $token) { throw 'GH_TOKEN is not set.' }
$headers = @{
    Authorization          = "Bearer $token"
    Accept                 = 'application/vnd.github+json'
    'User-Agent'           = 'dafeiyu-winget'
    'X-GitHub-Api-Version' = '2022-11-28'
}

function Api {
    param([string]$Method, [string]$Uri, $Payload)
    $args = @{ Method = $Method; Uri = $Uri; Headers = $headers }
    if ($null -ne $Payload) {
        $args.Body = [System.Text.Encoding]::UTF8.GetBytes(($Payload | ConvertTo-Json -Depth 6))
        $args.ContentType = 'application/json; charset=utf-8'
    }
    Invoke-RestMethod @args
}

# --- 1. fork (idempotent) --------------------------------------------------
$exists = $null
try { $exists = Api GET "https://api.github.com/repos/$fork" } catch { $exists = $null }

if (-not $exists) {
    Write-Host "forking $upstream ... (this can take a moment)"
    try { Api POST "https://api.github.com/repos/$upstream/forks" @{} | Out-Null } catch { }
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 5
        try { $exists = Api GET "https://api.github.com/repos/$fork"; break } catch { }
        Write-Host "  waiting for the fork ... ($($i + 1)/30)"
    }
} else {
    Write-Host "fork already exists"
}
if (-not $exists) { throw "fork did not become available: $fork" }
Write-Host "fork ready"

# --- 2. branch off master --------------------------------------------------
$branch = "dafeiyu-live-notify-$Version"
$base = Api GET "https://api.github.com/repos/$fork/git/ref/heads/master"
$baseSha = $base.object.sha

$branchExists = $null
try { $branchExists = Api GET "https://api.github.com/repos/$fork/git/ref/heads/$branch" } catch { $branchExists = $null }

if ($branchExists) {
    Write-Host "branch $branch already exists - reusing"
} else {
    Api POST "https://api.github.com/repos/$fork/git/refs" @{
        ref = "refs/heads/$branch"; sha = $baseSha
    } | Out-Null
    Write-Host "branch $branch created"
}

# --- 3. upload the manifest files -----------------------------------------
foreach ($f in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $content = [Convert]::ToBase64String($bytes)

    $existing = $null
    try {
        $existing = Api GET "https://api.github.com/repos/$fork/contents/$destDir/$($f.Name)?ref=$branch"
    } catch { $existing = $null }

    $payload = @{ message = "Add $identifier version $Version"; content = $content; branch = $branch }
    if ($existing) { $payload.sha = $existing.sha }

    Api PUT "https://api.github.com/repos/$fork/contents/$destDir/$($f.Name)" $payload | Out-Null
    Write-Host "  uploaded $($f.Name)"
}

# --- 4. open the pull request ---------------------------------------------
$prTitle = "New version: $identifier version $Version"
$prBody = @"
### Package

- Identifier: ``$identifier``
- Version: ``$Version``
- Installer: ``zip`` + nested ``portable``

### What it is

$shortDesc

A small Windows helper: it watches your Bilibili live room and, the moment the
room actually goes live, posts an ``@all`` notification to your QQ groups. It
also names the game you are playing, attaches the stream cover, and sends a
follow-up reminder after 30 and 60 minutes.

### Notes for reviewers

- The package is a portable zip; ``NestedInstallerFiles`` points at the exe
  inside it and the command alias is ``dafeiyu``.
- ``winget validate --manifest`` passes locally.
- The app talks to a local OneBot (NapCat) instance over ``127.0.0.1``. NapCat
  itself is not bundled (its licence forbids commercial redistribution and it
  updates frequently), so the README documents fetching it separately. That is
  a post-install setup step for one optional trigger source, not a requirement
  for the installer to succeed.

Release: https://github.com/$me/dafeiyu-live-notify/releases/tag/v$Version
"@

$pr = $null
try {
    $pr = Api POST "https://api.github.com/repos/$upstream/pulls" @{
        title = $prTitle
        head  = "$($me):$branch"
        base  = 'master'
        body  = $prBody
    }
    Write-Host ''
    Write-Host "[created] $($pr.html_url)"
} catch {
    # A PR for this branch already exists. Pushing new commits updates it in
    # place, which is exactly what re-running this script is for - so find it
    # and report it instead of failing.
    $found = Api GET "https://api.github.com/repos/$upstream/pulls?head=$($me):$branch&state=all"
    if (@($found).Count -eq 0) { throw }
    $pr = @($found)[0]
    Write-Host ''
    Write-Host "[updated] PR already existed - new commits pushed to the branch"
    Write-Host "          $($pr.html_url)"
}

Write-Host ''
Write-Host "Remember: the Microsoft CLA must be signed by YOU, in a comment on the PR:"
Write-Host "    @microsoft-github-policy-service agree"

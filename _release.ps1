# ===========================================================================
#  Publish a GitHub Release
# ===========================================================================
#  Why this exists: **pushing a tag is not enough.** GitHub's Releases page
#  only shows releases created through the API. A plain tag lives in the Tags
#  list, so anyone opening /releases sees an empty page and cannot tell what
#  changed between versions.
#
#  Usage:
#      $env:GH_TOKEN = '<token with repo scope>'
#      .\_release.ps1                 # version read from live_notify.py
#      .\_release.ps1 -Version 1.2.0  # or pass it explicitly
#      .\_release.ps1 -DryRun         # print what would be sent, no API call
#
#  The body is **extracted from CHANGELOG.md** (the section with the matching
#  version), so the changelog and the release can never drift apart - edit the
#  changelog and you are done.
#
#  Push the tag first: git push origin --tags
#
#  KEEP THIS FILE PURE ASCII.
#  Windows PowerShell 5.1 decodes BOM-less script files using the ANSI
#  codepage; UTF-8 Chinese comments get mangled and the parser dies. The `edit`
#  tool also strips BOMs, so a file that works today can break on the next
#  edit. English messages sidestep all of it - and the Chinese would have come
#  out as mojibake in this console anyway.
#
#  Never put a token in this file. It only ever reads $env:GH_TOKEN.
# ===========================================================================

param(
    [string]$Version,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# --- 1. version: from the parameter, else from live_notify.py --------------
if (-not $Version) {
    $src = Get-Content (Join-Path $root 'live_notify.py') -Raw -Encoding UTF8
    $m = [regex]::Match($src, 'VERSION\s*=\s*"([^"]+)"')
    if (-not $m.Success) { throw 'VERSION not found in live_notify.py' }
    $Version = $m.Groups[1].Value
}
$tag = "v$Version"
Write-Host "version: $Version    tag: $tag"

# --- 2. pull the matching section out of CHANGELOG.md ----------------------
$changelogPath = Join-Path $root 'CHANGELOG.md'
$changelog = Get-Content $changelogPath -Raw -Encoding UTF8
$pattern = '(?ms)^##\s+' + [regex]::Escape($Version) + '\s*\r?\n(.*?)(?=^##\s|\z)'
$hit = [regex]::Match($changelog, $pattern)
if (-not $hit.Success) {
    throw "CHANGELOG.md has no '## $Version' section. Add one before releasing."
}
$body = $hit.Groups[1].Value.Trim()
Write-Host "body extracted from CHANGELOG: $($body.Length) chars"

# Title = first line of the section, trailing punctuation trimmed, capped.
# The unicode escapes keep this file ASCII-only.
$firstLine = ($body -split "`n")[0].Trim()
$firstLine = $firstLine -replace '[\u3002\uFF01\uFF0C\uFF1B.,!;]+$', ''
if ($firstLine.Length -gt 40) { $firstLine = $firstLine.Substring(0, 40) + [char]0x2026 }
$dash = [string][char]0x2014 + [string][char]0x2014
$title = "$Version $dash $firstLine"

if ($DryRun) {
    Write-Host ''
    Write-Host '----- title -----'
    Write-Host $title
    Write-Host '----- body -----'
    Write-Host $body
    return
}

# --- 3. create or update through the API -----------------------------------
$token = $env:GH_TOKEN
if (-not $token) {
    throw 'GH_TOKEN is not set. Run: $env:GH_TOKEN = ''...'''
}

$api = 'https://api.github.com/repos/KagurazakaChizuru/dafeiyu-live-notify/releases'
$headers = @{
    Authorization          = "Bearer $token"
    Accept                 = 'application/vnd.github+json'
    'User-Agent'           = 'dafeiyu-release'
    'X-GitHub-Api-Version' = '2022-11-28'
}

function Send-Json {
    param([string]$Method, [string]$Uri, [hashtable]$Payload)
    # Convert to UTF-8 bytes explicitly, otherwise the Chinese body is mangled.
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($Payload | ConvertTo-Json -Depth 4))
    Invoke-RestMethod -Method $Method -Uri $Uri -Headers $headers `
                      -Body $bytes -ContentType 'application/json; charset=utf-8'
}

# Updating an existing release keeps this script safe to re-run.
$existing = $null
try {
    $existing = Invoke-RestMethod -Uri "$api/tags/$tag" -Headers $headers
} catch {
    $existing = $null
}

if ($existing) {
    $r = Send-Json -Method Patch -Uri "$api/$($existing.id)" -Payload @{
        name = $title; body = $body; draft = $false; prerelease = $false
    }
    Write-Host "[updated] $tag  ->  $($r.html_url)"
} else {
    $r = Send-Json -Method Post -Uri $api -Payload @{
        tag_name = $tag; name = $title; body = $body
        draft = $false; prerelease = $false
    }
    Write-Host "[created] $tag  ->  $($r.html_url)"
}

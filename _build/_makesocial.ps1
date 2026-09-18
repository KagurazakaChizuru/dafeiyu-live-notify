# ===========================================================================
#  生成 GitHub Social preview 图（1280 x 640）
# ===========================================================================
#  GitHub 的 social preview **没有 API**，只能在仓库 Settings 里手动上传。
#  这个脚本负责把图生成出来，省得每次重做。
#
#  用法：
#      powershell -ExecutionPolicy Bypass -File _build/_makesocial.ps1
#      然后到 Settings -> Social preview -> Upload an image
#
#  依赖 Windows 自带的 GDI+，不需要 Pillow。
#
#  注意：本文件带 BOM，改完记得确认 BOM 还在（编辑工具会吃掉它），
#  否则 PowerShell 5.1 会按 ANSI 解码、中文注释被拆坏后直接报语法错。
# ===========================================================================

param(
    [string]$Icon  = "$PSScriptRoot\app.ico",
    [string]$Out   = "$PSScriptRoot\social-preview.png"
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$W = 1280
$H = 640
$NAVY   = [System.Drawing.Color]::FromArgb(255, 27, 42, 74)
$BLUE   = [System.Drawing.Color]::FromArgb(255, 46, 78, 143)
$YELLOW = [System.Drawing.Color]::FromArgb(255, 245, 179, 1)
$DIM    = [System.Drawing.Color]::FromArgb(255, 157, 184, 228)
$TEXT   = [System.Drawing.Color]::FromArgb(255, 214, 225, 241)
$FONT   = 'Microsoft YaHei UI'

$bmp = New-Object System.Drawing.Bitmap($W, $H)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode        = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.InterpolationMode    = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.TextRenderingHint    = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
$g.PixelOffsetMode      = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality

# --- 背景：斜向渐变 ---------------------------------------------------------
$rect = New-Object System.Drawing.Rectangle(0, 0, $W, $H)
$bg = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        $rect, $NAVY, $BLUE, 25.0)
$g.FillRectangle($bg, $rect)
$bg.Dispose()

# --- 左上角一点高光，免得整块死板 -------------------------------------------
$glow = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.Rectangle(0, 0, 700, 300)),
        [System.Drawing.Color]::FromArgb(38, 255, 255, 255),
        [System.Drawing.Color]::FromArgb(0, 255, 255, 255), 90.0)
$g.FillRectangle($glow, 0, 0, 700, 300)
$glow.Dispose()

# --- 图标 -------------------------------------------------------------------
# 直接把 app.ico 丢给 System.Drawing.Icon 会渲染成一片彩色雪花：
# GDI+ 的 Icon 类读不了 ICO 里 **PNG 压缩**的帧，而我们的 app.ico 正是
# PNG 载荷（见 _makeicon.ps1）。所以自己解析 ICO 目录，把最大的那张
# PNG 掏出来交给 GDI+。
function Get-IconPngBytes {
    param([string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $reserved = [BitConverter]::ToUInt16($bytes, 0)
    $type     = [BitConverter]::ToUInt16($bytes, 2)
    if ($reserved -ne 0 -or $type -ne 1) { throw "不是合法的 ICO：$Path" }
    $count = [BitConverter]::ToUInt16($bytes, 4)

    $bestOff = -1; $bestLen = 0; $bestW = 0
    for ($i = 0; $i -lt $count; $i++) {
        $o = 6 + $i * 16
        $w = [int]$bytes[$o]; if ($w -eq 0) { $w = 256 }      # 0 表示 256
        $len = [BitConverter]::ToUInt32($bytes, $o + 8)
        $off = [BitConverter]::ToUInt32($bytes, $o + 12)
        if ($off + 8 -gt $bytes.Length) { continue }
        $isPng = ($bytes[$off] -eq 0x89 -and $bytes[$off + 1] -eq 0x50 -and
                  $bytes[$off + 2] -eq 0x4E -and $bytes[$off + 3] -eq 0x47)
        if ($isPng -and $w -gt $bestW) { $bestW = $w; $bestOff = $off; $bestLen = $len }
    }
    if ($bestOff -lt 0) { throw "这个 ICO 里没有 PNG 帧：$Path" }
    $out = New-Object byte[] $bestLen
    [Array]::Copy($bytes, $bestOff, $out, 0, $bestLen)
    return ,$out
}

$pngBytes = Get-IconPngBytes -Path $Icon
$ms = New-Object System.IO.MemoryStream(, $pngBytes)
$iconBmp = [System.Drawing.Image]::FromStream($ms)

$size = 340
$iconX = 70
$iconY = [int](($H - $size) / 2)
$g.DrawImage($iconBmp, (New-Object System.Drawing.Rectangle($iconX, $iconY, $size, $size)))
$iconBmp.Dispose()
$ms.Dispose()

# --- 文字 -------------------------------------------------------------------
$x = 470

$fTitle = New-Object System.Drawing.Font($FONT, 58, [System.Drawing.FontStyle]::Bold)
$g.DrawString('大肥鱼直播姬', $fTitle, [System.Drawing.Brushes]::White, $x, 150)
$fTitle.Dispose()

$fSub = New-Object System.Drawing.Font($FONT, 25)
$g.DrawString('开播自动通知 QQ 群', $fSub, (New-Object System.Drawing.SolidBrush($DIM)), $x + 4, 245)
$fSub.Dispose()

# 一条黄色分隔线
$pen = New-Object System.Drawing.Pen($YELLOW, 4)
$g.DrawLine($pen, $x + 4, 300, $x + 120, 300)
$pen.Dispose()

$fBullet = New-Object System.Drawing.Font($FONT, 22)
$bullets = @(
    '自动识别在玩什么游戏',
    '开播通知带直播间封面',
    'B站轮询 / OBS 事件 / 快捷键',
    '绿色免安装 · 零第三方依赖'
)
$y = 340
foreach ($b in $bullets) {
    $g.DrawString([char]0x2022 + '  ' + $b, $fBullet,
                  (New-Object System.Drawing.SolidBrush($TEXT)), $x + 4, $y)
    $y += 50
}
$fBullet.Dispose()

# --- 右下角仓库地址 ---------------------------------------------------------
$fFoot = New-Object System.Drawing.Font('Consolas', 17)
$foot = 'github.com/KagurazakaChizuru/dafeiyu-live-notify'
$sz = $g.MeasureString($foot, $fFoot)
$g.DrawString($foot, $fFoot, (New-Object System.Drawing.SolidBrush($DIM)),
              ($W - $sz.Width - 40), ($H - $sz.Height - 28))
$fFoot.Dispose()

$g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()

Write-Host "已生成: $Out  ($([math]::Round((Get-Item $Out).Length/1KB,1)) KB, ${W}x${H})"
Write-Host "上传位置: 仓库 Settings -> Social preview -> Upload an image"

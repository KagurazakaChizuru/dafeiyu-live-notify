# ===========================================================================
#  Build the social preview / README banner
# ===========================================================================
#  1280x640, which is GitHub's recommended social preview size (2:1).
#
#  Background is the key art (_build\banner-art.png). Two things matter:
#
#   1. The art is shifted RIGHT so the character sits around 72% of the
#      width. She is near the centre in the source, and the text block lives
#      on the left - centring her would put the title straight across her face.
#
#   2. The scrim is hold-then-fade, not a straight linear ramp. A linear fade
#      is still ~35% opaque where the last bullet ends, which muddies white
#      text and washes over the character. Solid through the text, gone
#      before her.
#
#  Keep this file BOM-less UTF-8 only if it stays ASCII; it has Chinese
#  literals, so it MUST be saved as UTF-8 WITH BOM or PowerShell 5.1 reads
#  it as ANSI and the text turns to mojibake.
# ===========================================================================
Add-Type -AssemblyName System.Drawing

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$art  = Join-Path $here 'banner-art.png'
$icon = Join-Path $here 'app.ico'
$out  = Join-Path $repo 'docs\images\banner.jpg'

$W = 1280
$H = 640

$AZURE  = [System.Drawing.Color]::FromArgb(255, 102, 204, 255)   # 天依蓝
$SCRIM  = [System.Drawing.Color]::FromArgb(255, 20, 32, 58)      # 深海军蓝
$DIM    = [System.Drawing.Color]::FromArgb(255, 178, 202, 236)
$WHITE  = [System.Drawing.Color]::White

$bmp = New-Object System.Drawing.Bitmap($W, $H)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

# ---- background: cover-fit, pushed right so the character clears the text --
$img = [System.Drawing.Image]::FromFile($art)
$scale = [Math]::Max($W / $img.Width, $H / $img.Height)
$dw = [int]($img.Width * $scale)
$dh = [int]($img.Height * $scale)
$dx = [int](($W - $dw) * 0.5)
$dy = [int](($H - $dh) * 0.42)

# **水平翻转，跟 App 顶部那张头图保持一致。**
#
# 注意 $dx 那行其实是无效的：原图缩到目标宽度时 $dw 恰好等于 $W，
# 横向根本没有余量，$dx 恒为 0。想让角色换边只能靠翻转 ——
# 她本来在画面中间偏左，翻过来就落在中间偏右，朝向也跟着一致了。
#
# 翻转只作用于插画。**遮罩不能跟着翻** —— 文字在左边，遮罩就得留在左边，
# 跟着翻的话角色正好落到文字底下。
$g.TranslateTransform($W, 0)
$g.ScaleTransform(-1, 1)
$g.DrawImage($img, $dx, $dy, $dw, $dh)
$g.ResetTransform()
$img.Dispose()

# ---- scrim: solid across the text, then fade out ---------------------------
$scrimW = 820
$solid  = [System.Drawing.Color]::FromArgb(238, $SCRIM.R, $SCRIM.G, $SCRIM.B)
$clear  = [System.Drawing.Color]::FromArgb(0,   $SCRIM.R, $SCRIM.G, $SCRIM.B)
$rect   = New-Object System.Drawing.Rectangle(0, 0, $scrimW, $H)
$br = New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect, $solid, $clear, [System.Drawing.Drawing2D.LinearGradientMode]::Horizontal)
$blend = New-Object System.Drawing.Drawing2D.ColorBlend(3)
$blend.Colors = @($solid, $solid, $clear)
$blend.Positions = @(0.0, 0.46, 1.0)
$br.InterpolationColors = $blend
$g.FillRectangle($br, $rect)
$br.Dispose()

# a touch of darkening at the very bottom, so the URL never sits on bright water
# New-Object 的参数里**不能出现算术** —— PS 5.1 会把 "$H - 120" 拆成多个参数，
# 报 "Cannot find an overload ... argument count"。先算进变量再传。
$fadeY = $H - 120
$bfade = New-Object System.Drawing.Rectangle -ArgumentList @([int]0, [int]$fadeY, [int]$W, [int]120)
$cTop = [System.Drawing.Color]::FromArgb(0, 0, 0, 0)
$cBot = [System.Drawing.Color]::FromArgb(130, 8, 14, 28)
$bf = New-Object System.Drawing.Drawing2D.LinearGradientBrush -ArgumentList @(
    $bfade, $cTop, $cBot, [System.Drawing.Drawing2D.LinearGradientMode]::Vertical)
$g.FillRectangle($bf, $bfade)
$bf.Dispose()

# ---- app icon, small, above the title --------------------------------------
#
# **不要用 DrawIcon。** Icon 的构造函数会自己挑一帧 —— 实测它挑了 32x32，
# 再放大到 96x96 就成了一块噪点。app.ico 里的七帧都是 PNG 压缩的，
# 直接把最大的那帧提出来当 PNG 画，才是它本来的样子。
function Get-IconPng {
    param([string]$Path, [string]$OutPng)
    $b = [System.IO.File]::ReadAllBytes($Path)
    $count = [BitConverter]::ToUInt16($b, 4)
    $bestSize = 0; $bestOff = 0
    for ($i = 0; $i -lt $count; $i++) {
        $o = 6 + $i * 16
        $size = [BitConverter]::ToUInt32($b, $o + 8)
        $off = [BitConverter]::ToUInt32($b, $o + 12)
        # 前 8 字节是 PNG 签名才算数
        if ($b[$off] -eq 0x89 -and $b[$off + 1] -eq 0x50 -and $size -gt $bestSize) {
            $bestSize = $size; $bestOff = $off
        }
    }
    if ($bestSize -eq 0) { return $false }
    $frame = New-Object byte[] $bestSize
    [Array]::Copy($b, $bestOff, $frame, 0, $bestSize)
    [System.IO.File]::WriteAllBytes($OutPng, $frame)
    return $true
}

try {
    $iconPng = Join-Path $env:TEMP 'dfy-banner-icon.png'
    if (Get-IconPng -Path $icon -OutPng $iconPng) {
        $ii = [System.Drawing.Image]::FromFile($iconPng)
        $g.DrawImage($ii, (New-Object System.Drawing.Rectangle(88, 76, 96, 96)))
        $ii.Dispose()
        Write-Host "  icon drawn from $($icon)"
    } else {
        Write-Host "  (icon: no PNG frame found, skipped)"
    }
} catch {
    Write-Host "  (icon skipped: $($_.Exception.Message))"
}

# ---- text ------------------------------------------------------------------
$fTitle = New-Object System.Drawing.Font('Microsoft YaHei', 52, [System.Drawing.FontStyle]::Bold)
$fSub   = New-Object System.Drawing.Font('Microsoft YaHei', 24)
$fBody  = New-Object System.Drawing.Font('Microsoft YaHei', 23)
$fUrl   = New-Object System.Drawing.Font('Consolas', 19)
$bTitle = New-Object System.Drawing.SolidBrush($WHITE)
$bDim   = New-Object System.Drawing.SolidBrush($DIM)

$g.DrawString('大肥鱼直播姬', $fTitle, $bTitle, 86, 190)
$g.DrawString('开播自动通知 QQ 群', $fSub, $bDim, 90, 268)

$pen = New-Object System.Drawing.Pen($AZURE, 5)
$g.DrawLine($pen, 90, 318, 206, 318)
$pen.Dispose()

$bullets = @(
    '自动识别在玩什么游戏',
    '开播通知带直播间封面',
    '13 套文案自动轮换，刷不腻',
    'B站轮询 / OBS 事件 / 快捷键',
    '绿色免安装 · 零第三方依赖'
)
$y = 356
foreach ($b in $bullets) {
    $g.FillEllipse($bDim, 92, ($y + 11), 7, 7)
    $g.DrawString($b, $fBody, $bDim, 114, $y)
    $y += 40
}

# URL 放左下、进遮罩区 —— 放右下会压在亮水面上，实测读不清
$url = 'github.com/KagurazakaChizuru/dafeiyu-live-notify'
$g.DrawString($url, $fUrl, $bDim, 92, 584)

$fTitle.Dispose(); $fSub.Dispose(); $fBody.Dispose(); $fUrl.Dispose()
$bTitle.Dispose(); $bDim.Dispose()
$g.Dispose()

# ---- 存 JPEG ---------------------------------------------------------------
#
# **不能存 PNG。** 这张图 1.7 MB，而 GitHub 的社交预览有 1 MB 上限。
# PNG 是无损压缩，对这种照片类的画面本来就不划算 —— 水下全是平滑渐变。
#
# q92 是试出来的：再高体积涨得快，再低文字的边缘会出现可见的振铃。
$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() |
         Where-Object { $_.MimeType -eq 'image/jpeg' }
$encParams = New-Object System.Drawing.Imaging.EncoderParameters -ArgumentList @([int]1)
$encParams.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter -ArgumentList @(
    [System.Drawing.Imaging.Encoder]::Quality, [int64]92)
$bmp.Save($out, $codec, $encParams)
$bmp.Dispose()

$kb = [Math]::Round((Get-Item $out).Length/1KB, 1)
Write-Host "已生成: $out  (${kb} KB, ${W}x${H})"
if ($kb -gt 1000) {
    Write-Host "  [WARN] 超过 1 MB，GitHub 社交预览会拒收。把质量降到 88 再试。"
} else {
    Write-Host "  OK: under the 1 MB social-preview limit"
}
Write-Host "上传位置: 仓库 Settings -> Social preview -> Upload an image"

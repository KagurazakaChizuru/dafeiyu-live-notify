# ===========================================================================
#  从一张方形插画生成多尺寸 app.ico
# ===========================================================================
#  用法：
#      powershell -ExecutionPolicy Bypass -File _makeicon.ps1 -Source D:\art.png
#
#  为什么要用 PowerShell 而不是 Python：
#      Python 标准库没有图像缩放能力（要装 Pillow），而 GDI+ 的
#      HighQualityBicubic 缩放质量足够好，且 Windows 自带。
#
#  两个踩过的坑，改动时注意：
#    1. PowerShell 函数 return 数组会被**自动展开**，byte[] 会变成 object[]，
#       导致 BinaryWriter.Write 找不到重载、静默写不进去（ICO 只有 118 字节）。
#       所以这里不写函数，直接落临时 PNG 文件，再用 ReadAllBytes 读回来。
#    2. 组装的载荷数组必须显式声明成 [byte[][]]，否则 += 同样会展开。
# ===========================================================================

param(
    [Parameter(Mandatory = $true)][string]$Source,
    [string]$Output = "$PSScriptRoot\app.ico",
    [double]$Radius = 0.14,          # 圆角半径（占边长比例），设 0 则直角
    [int[]]$Sizes = @(256, 128, 64, 48, 32, 24, 16),
    [switch]$Preview                # 额外输出一张多尺寸预览图
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

if (-not (Test-Path $Source)) { throw "找不到源图：$Source" }

$src = [System.Drawing.Image]::FromFile($Source)
Write-Host "源图: $Source  ($($src.Width) x $($src.Height))"

$tmp = Join-Path $env:TEMP ("iconsizes_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null

try {
    foreach ($s in $Sizes) {
        $bmp = New-Object System.Drawing.Bitmap($s, $s, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode      = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.InterpolationMode  = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.PixelOffsetMode    = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
        $g.Clear([System.Drawing.Color]::Transparent)

        if ($Radius -gt 0) {
            $r = [double]$s * $Radius
            $d = $r * 2
            $path = New-Object System.Drawing.Drawing2D.GraphicsPath
            $path.AddArc(0, 0, $d, $d, 180, 90)
            $path.AddArc($s - $d, 0, $d, $d, 270, 90)
            $path.AddArc($s - $d, $s - $d, $d, $d, 0, 90)
            $path.AddArc(0, $s - $d, $d, $d, 90, 90)
            $path.CloseFigure()
            $g.SetClip($path)
            $g.DrawImage($src, (New-Object System.Drawing.Rectangle(0, 0, $s, $s)))
            $path.Dispose()
        } else {
            $g.DrawImage($src, (New-Object System.Drawing.Rectangle(0, 0, $s, $s)))
        }
        $g.Dispose()

        $bmp.Save((Join-Path $tmp "$s.png"), [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        Write-Host ("  {0,3}x{0,-3}  {1,7} 字节" -f $s, (Get-Item (Join-Path $tmp "$s.png")).Length)
    }
    $src.Dispose()

    # 组装 ICO（显式 [byte[][]]，避免 PowerShell 展开数组）
    [byte[][]]$payloads = @()
    foreach ($s in $Sizes) {
        $payloads += ,([System.IO.File]::ReadAllBytes((Join-Path $tmp "$s.png")))
    }

    $fs = [System.IO.File]::Create($Output)
    $bw = New-Object System.IO.BinaryWriter($fs)
    $bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]$Sizes.Count)
    $offset = 6 + 16 * $Sizes.Count
    for ($i = 0; $i -lt $Sizes.Count; $i++) {
        $s = $Sizes[$i]
        $dim = if ($s -ge 256) { 0 } else { $s }
        $bw.Write([Byte]$dim); $bw.Write([Byte]$dim); $bw.Write([Byte]0); $bw.Write([Byte]0)
        $bw.Write([UInt16]1); $bw.Write([UInt16]32)
        $bw.Write([UInt32]$payloads[$i].Length); $bw.Write([UInt32]$offset)
        $offset += $payloads[$i].Length
    }
    foreach ($p in $payloads) { $bw.Write($p) }
    $bw.Flush(); $bw.Close(); $fs.Close()

    Write-Host "已生成: $Output  ($([math]::Round((Get-Item $Output).Length/1KB,1)) KB)"

    if ($Preview) {
        $sheetW = ($Sizes | Measure-Object -Sum).Sum + 10 * ($Sizes.Count + 1)
        $sheet = New-Object System.Drawing.Bitmap($sheetW, 280, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $sg = [System.Drawing.Graphics]::FromImage($sheet)
        $sg.Clear([System.Drawing.Color]::FromArgb(255, 246, 246, 250))
        $sg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
        $x = 10
        foreach ($s in $Sizes) {
            $im = [System.Drawing.Image]::FromFile((Join-Path $tmp "$s.png"))
            $sg.DrawImage($im, $x, 20, $s, $s)
            $im.Dispose()
            $x += $s + 10
        }
        $sg.Dispose()
        $sheet.Save("$PSScriptRoot\icon-preview.png", [System.Drawing.Imaging.ImageFormat]::Png)
        $sheet.Dispose()
        Write-Host "预览图: $PSScriptRoot\icon-preview.png"
    }
}
finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

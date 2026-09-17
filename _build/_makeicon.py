#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成程序图标 app.ico
====================

纯 Python 实现，不依赖 Pillow 等任何第三方库：
  · 用 zlib + struct 手写 PNG 编码器
  · 4 倍超采样做抗锯齿
  · 组装成多尺寸 ICO（内含 PNG，Windows Vista 以后都支持）

图案：圆角方块 + 渐变底色 + 白色「广播」符号（圆点 + 两道向上张开的弧）。

用法：  python _makeicon.py
输出：  同目录下的 app.ico 和 icon-preview.png
"""

import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))

SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 4                                   # 超采样倍数

# 渐变：上 -> 下（直播感的玫红）
TOP = (255, 123, 138)
BOTTOM = (224, 31, 79)
WHITE = (255, 255, 255)

CORNER = 0.22                            # 圆角半径（占边长比例）
DOT_C = (0.5, 0.665)                     # 广播符号的中心
DOT_R = 0.088
ARCS = [(0.185, 0.250, 58.0), (0.300, 0.365, 58.0)]   # (内径, 外径, 半张角)


# --------------------------------------------------------------------------
#  PNG 编码
# --------------------------------------------------------------------------

def png_encode(w, h, rgba):
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)                                  # filter: none
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
#  绘制
# --------------------------------------------------------------------------

def in_rounded(x, y, r):
    """点 (x, y)（归一化 0~1）是否落在圆角方块内。"""
    cx = min(max(x, r), 1.0 - r)
    cy = min(max(y, r), 1.0 - r)
    return math.hypot(x - cx, y - cy) <= r


def in_arc(x, y, c, r_in, r_out, half_angle):
    dx = x - c[0]
    dy = y - c[1]
    d = math.hypot(dx, dy)
    if not (r_in <= d <= r_out):
        return False
    # 以正上方为 0 度，向右为正
    ang = math.degrees(math.atan2(dx, -dy))
    return abs(ang) <= half_angle


def sample(x, y):
    """返回该点的 (r, g, b, a)。"""
    if not in_rounded(x, y, CORNER):
        return (0, 0, 0, 0)

    # 底色渐变
    t = y
    r = round(TOP[0] + (BOTTOM[0] - TOP[0]) * t)
    g = round(TOP[1] + (BOTTOM[1] - TOP[1]) * t)
    b = round(TOP[2] + (BOTTOM[2] - TOP[2]) * t)

    # 白色广播符号
    dx = x - DOT_C[0]
    dy = y - DOT_C[1]
    if math.hypot(dx, dy) <= DOT_R:
        return (WHITE[0], WHITE[1], WHITE[2], 255)
    for r_in, r_out, half in ARCS:
        if in_arc(x, y, DOT_C, r_in, r_out, half):
            return (WHITE[0], WHITE[1], WHITE[2], 255)

    return (r, g, b, 255)


def render(size):
    """渲染一个 size x size 的 RGBA 图（带回超采样）。"""
    big = size * SS
    acc = [[0, 0, 0, 0] for _ in range(size * size)]

    for by in range(big):
        y = (by + 0.5) / big
        row = by // SS
        for bx in range(big):
            x = (bx + 0.5) / big
            col = bx // SS
            r, g, b, a = sample(x, y)
            cell = acc[row * size + col]
            cell[0] += r * a
            cell[1] += g * a
            cell[2] += b * a
            cell[3] += a

    n = SS * SS
    out = bytearray(size * size * 4)
    for i, (sr, sg, sb, sa) in enumerate(acc):
        if sa == 0:
            out[i * 4:i * 4 + 4] = b"\x00\x00\x00\x00"
            continue
        a = sa // n
        # 用 alpha 加权还原颜色，避免边缘发黑
        out[i * 4 + 0] = min(255, sr // sa)
        out[i * 4 + 1] = min(255, sg // sa)
        out[i * 4 + 2] = min(255, sb // sa)
        out[i * 4 + 3] = min(255, a)
    return bytes(out)


# --------------------------------------------------------------------------
#  ICO 组装
# --------------------------------------------------------------------------

def build_ico(pngs, path):
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    blobs = b""
    for size, data in pngs:
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    with open(path, "wb") as fh:
        fh.write(header + entries + blobs)


def main():
    pngs = []
    for s in SIZES:
        rgba = render(s)
        data = png_encode(s, s, rgba)
        pngs.append((s, data))
        print("  渲染 {}x{}  ->  {} 字节".format(s, s, len(data)))

    ico = os.path.join(HERE, "app.ico")
    build_ico(pngs, ico)
    print("已生成:", ico, "({} 字节)".format(os.path.getsize(ico)))

    # 顺带存一张 256 预览图，方便肉眼检查
    preview = os.path.join(HERE, "icon-preview.png")
    with open(preview, "wb") as fh:
        fh.write(png_encode(256, 256, render(256)))
    print("预览图:", preview)


if __name__ == "__main__":
    main()

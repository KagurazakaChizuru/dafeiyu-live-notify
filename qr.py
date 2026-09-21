#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""二维码生成 —— **纯标准库**。

为什么要自己写一个：这个项目不引第三方依赖（"没有依赖的东西才能活得久"），
而 B站扫码登录需要把一条 131 字的 URL 画成二维码。所以只实现用得到的那一小块：

    · 字节模式（8 bit 一字符，UTF-8）
    · 纠错等级 **L**（网页登录不需要更强）
    · 版本 1 ~ 10（最大 271 字节，够放那条 URL 了）
    · 八种掩码按标准罚分挑最好的那个

规格照 ISO/IEC 18004。**没有实现**：数字/字母数字/汉字模式、等级 M/Q/H、
版本 11 以上、结构化追加 —— 用不上，加了就是没人测的死代码。

对外只有两个函数：
    encode(text) -> list[list[int]]   模块矩阵（不含静区），1 = 黑
    size_of(text) -> int              这条文字需要多大（调试用）
"""

#: 每个版本的数据码字、纠错码字、分块方式。只列等级 L。
#: 每项：(版本, [ (块数, 每块数据码字), ... ], 每块纠错码字)
_BLOCKS_L = {
    1: ([(1, 19)], 7),
    2: ([(1, 34)], 10),
    3: ([(1, 55)], 15),
    4: ([(1, 80)], 20),
    5: ([(1, 108)], 26),
    6: ([(2, 68)], 18),
    7: ([(2, 78)], 20),
    8: ([(2, 97)], 24),
    9: ([(2, 116)], 30),
    10: ([(2, 68), (2, 69)], 18),
}

#: 对齐全图案的坐标（版本 1 没有）。规格表原样抄，别自己推。
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

#: 等级 L 的格式信息位（15 bit，已含 BCH 与掩码 0x5412）
_FORMAT_L = {
    0: 0x77C4, 1: 0x72F3, 2: 0x7DAA, 3: 0x789D,
    4: 0x662F, 5: 0x6318, 6: 0x6C41, 7: 0x6976,
}

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _init_gf():
    """GF(256)，本原多项式 0x11D。纠错编码要用它做乘除。"""
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_gf()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(n):
    """生成多项式 (x - a^0)(x - a^1)...(x - a^(n-1))。"""
    poly = [1]
    for i in range(n):
        poly.append(0)
        for j in range(len(poly) - 1, 0, -1):
            poly[j] ^= _gf_mul(poly[j - 1], _GF_EXP[i])
    return poly


def _rs_ecc(data, n):
    """算 n 个纠错码字。就是多项式除法的余数。"""
    gen = _rs_generator(n)
    res = list(data) + [0] * n
    for i in range(len(data)):
        coef = res[i]
        if coef == 0:
            continue
        # gen[0] 恒为 1，所以这一圈顺带把 res[i] 自己异或成 0
        for j in range(len(gen)):
            res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data):]


def _bit_stream(text):
    """字节模式的码字流：模式指示符 + 计数 + 数据 + 结束符 + 填充。"""
    raw = text.encode("utf-8")
    for version in range(1, 11):
        blocks, _ecc = _BLOCKS_L[version]
        capacity = sum(cnt * size for cnt, size in blocks)
        # 计数指示符：版本 1~9 是 8 位，10 起是 16 位
        count_bits = 8 if version <= 9 else 16
        need = 4 + count_bits + len(raw) * 8
        if need <= capacity * 8:
            break
    else:
        raise ValueError("内容太长（{} 字节），这个实现只做到版本 10".format(len(raw)))

    bits = []

    def push(value, length):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                     # 字节模式
    push(len(raw), count_bits)
    for byte in raw:
        push(byte, 8)
    # 结束符最多 4 位，但不能超过容量 —— 超了就少放几位
    push(0, min(4, capacity * 8 - len(bits)))
    while len(bits) % 8:                # 补齐到整字节
        bits.append(0)
    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]
    # 交替填充字节，规格指定的两个值，**0xEC 打头**（从 0x11 开始就全错）
    pad = 0
    while len(codewords) < capacity:
        codewords.append(0xEC if pad % 2 == 0 else 0x11)
        pad += 1
    return version, codewords


def _interleave(version, codewords):
    """按分块把数据码字和纠错码字交错排好。"""
    blocks_spec, ecc_len = _BLOCKS_L[version]
    blocks, pos = [], 0
    for count, size in blocks_spec:
        for _ in range(count):
            chunk = codewords[pos:pos + size]
            pos += size
            blocks.append((chunk, _rs_ecc(chunk, ecc_len)))
    out = []
    for i in range(max(len(d) for d, _ in blocks)):     # 数据码字：按列取
        for data, _ecc in blocks:
            if i < len(data):
                out.append(data[i])
    for i in range(ecc_len):                            # 纠错码字：同样按列取
        for _data, ecc in blocks:
            out.append(ecc[i])
    return out


def _matrix(version, codewords):
    """建矩阵并填数据。返回 (矩阵, 该位置是不是功能图案)。"""
    n = version * 4 + 17
    m = [[0] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def put(r, c, val):
        if 0 <= r < n and 0 <= c < n:
            m[r][c] = val
            fixed[r][c] = True

    # 三个定位图案 + 分隔带（分隔带是**白**的，围着 7×7 一圈）
    for r, c in ((0, 0), (0, n - 7), (n - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if not (0 <= rr < n and 0 <= cc < n):
                    continue
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                inner = 2 <= dr <= 4 and 2 <= dc <= 4
                # ring 只在 7×7 内部成立 —— 忘了那个 inside 判断，
                # 外圈就会跟着 dr/dc ∈ (-1, 7) 被涂黑，分隔带没了。
                ring = inside and (dr in (0, 6) or dc in (0, 6))
                put(rr, cc, 1 if (inner or ring) else 0)
    # 定时图案
    for i in range(8, n - 8):
        put(6, i, 1 - (i % 2))
        put(i, 6, 1 - (i % 2))
    # 对齐全图案
    coords = _ALIGN[version]
    for r in coords:
        for c in coords:
            if (r, c) in ((6, 6), (6, n - 7), (n - 7, 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, 1 if max(abs(dr), abs(dc)) != 1 else 0)
    # 固定黑块（规格里那个"总是黑"的模块）
    put(n - 8, 8, 1)
    # 预留格式信息（值稍后填，先占位免得数据压上去）
    for i in range(9):
        if not fixed[8][i]:
            put(8, i, 0)
        if not fixed[i][8]:
            put(i, 8, 0)
    for i in range(8):
        put(8, n - 1 - i, 0)
        put(n - 1 - i, 8, 0)
    # 版本信息（版本 7 起才有）
    if version >= 7:
        bits = _version_bits(version)
        for i in range(18):
            bit = (bits >> i) & 1
            put(n - 11 + i % 3, i // 3, bit)
            put(i // 3, n - 11 + i % 3, bit)

    # 数据：从右下角起，两列一组、蛇形往上/往下
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    idx, up = 0, True
    col = n - 1
    while col > 0:
        if col == 6:                    # 第 6 列是定时图案，整列跳过
            col -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for c in (col, col - 1):
                if fixed[r][c]:
                    continue
                m[r][c] = bits[idx] if idx < len(bits) else 0
                idx += 1
        up = not up
        col -= 2
    return m, fixed


def _version_bits(version):
    """版本信息的 18 位（6 位版本 + 12 位 BCH）。"""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


def _penalty(m):
    """四种罚分，规格里的 N1~N4。选掩码就靠它。"""
    n = len(m)
    score = 0

    def runs(line):
        total, run, prev = 0, 1, line[0]
        for v in line[1:]:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    total += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            total += 3 + (run - 5)
        return total

    for i in range(n):
        score += runs(m[i])
        score += runs([m[r][i] for r in range(n)])

    for r in range(n - 1):              # 2x2 同色
        for c in range(n - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3

    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for i in range(n):                  # 像定位图案的那种条纹
        row = m[i]
        col = [m[r][i] for r in range(n)]
        for j in range(n - 10):
            if row[j:j + 11] in (pat1, pat2):
                score += 40
            if col[j:j + 11] in (pat1, pat2):
                score += 40

    dark = sum(sum(row) for row in m)
    percent = dark * 100 / (n * n)
    score += int(abs(percent - 50) // 5) * 10
    return score


def _apply_mask(m, fixed, mask):
    n = len(m)
    out = [row[:] for row in m]
    for r in range(n):
        for c in range(n):
            if fixed[r][c]:
                continue
            if mask == 0:
                flip = (r + c) % 2 == 0
            elif mask == 1:
                flip = r % 2 == 0
            elif mask == 2:
                flip = c % 3 == 0
            elif mask == 3:
                flip = (r + c) % 3 == 0
            elif mask == 4:
                flip = (r // 2 + c // 3) % 2 == 0
            elif mask == 5:
                flip = (r * c) % 2 + (r * c) % 3 == 0
            elif mask == 6:
                flip = ((r * c) % 2 + (r * c) % 3) % 2 == 0
            else:
                flip = ((r + c) % 2 + (r * c) % 3) % 2 == 0
            if flip:
                out[r][c] ^= 1
    return out


def _place_format(m, mask):
    """把格式信息的 15 位摆到两个位置（两处内容一样，是冗余）。

    **横竖两轴别混**：第 8 列是"竖排"，第 8 行是"横排"，两者的下标规则不同。
    实测把这两组写反过 —— 数据区完全一致，只有第 8 行/第 8 列上几十格不对，
    而二维码照样能扫（纠错兜住了），属于"看着对、其实错"的那种。
    """
    n = len(m)
    bits = _FORMAT_L[mask]
    for i in range(15):
        bit = (bits >> i) & 1
        # 竖排：第 8 列
        if i < 6:
            m[i][8] = bit
        elif i < 8:
            m[i + 1][8] = bit
        else:
            m[n - 15 + i][8] = bit
        # 横排：第 8 行
        if i < 8:
            m[8][n - i - 1] = bit
        elif i == 8:
            m[8][7] = bit
        else:
            m[8][14 - i] = bit
    m[n - 8][8] = 1                     # 固定黑块，别被格式信息盖掉
    return m


def encode(text, border=0, mask=None):
    """把 text 编成二维码矩阵。1 = 黑，0 = 白。

    border 是静区宽度（规格要求 4；界面自己留边就不用再加）。
    mask 是给测试用的：指定 0~7 强制用某个掩码，正常传 None（按罚分自动挑）。
    """
    if not text:
        raise ValueError("内容是空的")
    version, codewords = _bit_stream(text)
    grid, fixed = _matrix(version, _interleave(version, codewords))
    if mask is not None:
        return _finish(grid, fixed, int(mask) % 8, border)
    best, best_score = None, None
    for candidate in range(8):
        cand = _place_format(_apply_mask(grid, fixed, candidate), candidate)
        score = _penalty(cand)
        if best_score is None or score < best_score:
            best, best_score = cand, score
    return _with_border(best, border)


def _finish(grid, fixed, mask, border):
    return _with_border(_place_format(_apply_mask(grid, fixed, mask), mask), border)


def _with_border(grid, border):
    if not border:
        return grid
    n = len(grid)
    out = [[0] * (n + border * 2) for _ in range(border)]
    for row in grid:
        out.append([0] * border + list(row) + [0] * border)
    out += [[0] * (n + border * 2) for _ in range(border)]
    return out


def size_of(text):
    """这条文字用到的版本与边长（调试和测试用）。"""
    version, _cw = _bit_stream(text)
    return version, version * 4 + 17


if __name__ == "__main__":
    import sys
    data = sys.argv[1] if len(sys.argv) > 1 else "hello"
    ver, size = size_of(data)
    grid = encode(data)
    print("版本 {}，{}×{}".format(ver, size, size))
    for row in grid:
        print("".join("##" if v else "  " for v in row))

"""
闲沐盒子 - 轻量二维码编码器 (纯 MicroPython)

用途: 本机设置 > HTTP服务 二维码子页, 把 "http://IP/" 编成可扫码矩阵。

能力:
  - byte 模式, Version 1~3, ECC 级别 M (单块, 无交织)
  - Version 3-M 数据码字 44 字节, 覆盖 "http://192.168.100.100:8080/" 类 URL
  - 固定 mask 0 (format 信息如实声明, 主流扫码器均可识别)
  - 返回的矩阵四边自带 2 模块空白 (绘制时可再叠加静区)

接口:
  get_matrix(text) -> (n, rows)
      n    矩阵边长 (模块数, 含每边 2 模块空白): V1=25 / V2=29 / V3=33
      rows 按行的 0/1 列表, rows[y][x] = 1 表示深色模块
  编码失败 (超长/参数异常) 返回 (None, None)
"""

# ---- GF(256) 有限域 (本原多项式 0x11D) ----
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _init_gf():
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_gen(n):
    """Reed-Solomon 生成多项式 ∏(x - α^i), 最高次在前, 首项系数恒为 1"""
    g = [1]
    for i in range(n):
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= _gf_mul(c, _GF_EXP[i])
        g = ng
    return g


def _rs_ec(data, n):
    """数据码字 -> n 个纠错码字"""
    g = _rs_gen(n)
    res = list(data) + [0] * n
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j in range(1, len(g)):
                res[i + j] ^= _gf_mul(g[j], coef)
    return res[len(data):]


# ---- 版本参数 (M 级): 数据码字 / 纠错码字 / 对齐图案中心 ----
# (data, ec, align_center, remainder_bits)
_VERSIONS = (
    (16, 10, None, 0),   # V1
    (28, 16, 18,   7),   # V2
    (44, 26, 22,   7),   # V3
)


def _build_data(text, ndata):
    """bit 流 -> 数据码字 (byte 模式, 含终止符与填充)"""
    payload = text.encode("utf-8")
    if len(payload) > 255:
        return None
    # 模式指示符 0100 (byte 模式) + 8 位字符计数
    bits = [0, 1, 0, 0]
    n = len(payload)
    for i in range(7, -1, -1):
        bits.append((n >> i) & 1)
    for b in payload:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)
    # 终止符 (最多 4 位 0)
    cap = ndata * 8
    for _ in range(4):
        if len(bits) < cap:
            bits.append(0)
    # 补齐到字节边界
    while len(bits) % 8:
        bits.append(0)
    codewords = []
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        codewords.append(v)
    # 填充码字 0xEC / 0x11 交替
    pad = 0xEC
    while len(codewords) < ndata:
        codewords.append(pad)
        pad = 0x11 if pad == 0xEC else 0xEC
    return codewords


def _draw_finder(m, size, cx, cy):
    """左上角在 (cx, cy) 的 7x7 定位图案 + 分隔线"""
    for dy in range(-1, 8):
        for dx in range(-1, 8):
            x = cx + dx
            y = cy + dy
            if x < 0 or y < 0 or x >= size or y >= size:
                continue
            if 0 <= dx <= 6 and 0 <= dy <= 6:
                edge = dx in (0, 6) or dy in (0, 6)
                core = 2 <= dx <= 4 and 2 <= dy <= 4
                m[y][x] = 1 if (edge or core) else 0
            else:
                m[y][x] = 0  # 分隔线


def _draw_alignment(m, size, cx, cy):
    """5x5 对齐图案, 中心 (cx, cy)"""
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            edge = abs(dx) == 2 or abs(dy) == 2
            core = dx == 0 and dy == 0
            m[cy + dy][cx + dx] = 1 if (edge or core) else 0


def _draw_format(m, size, mask):
    """format 信息两份 + 固定暗模块 (布局参照 qrcodegen)"""
    data = (0 << 3) | mask          # ECC M = 00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    fmt = ((data << 10) | rem) ^ 0x5412

    def bit(i):
        return (fmt >> i) & 1

    for i in range(6):
        m[i][8] = bit(i)
    m[7][8] = bit(6)
    m[8][8] = bit(7)
    m[8][7] = bit(8)
    for i in range(9, 15):
        m[8][14 - i] = bit(i)
    for i in range(8):
        m[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        m[size - 15 + i][8] = bit(i)
    m[size - 8][8] = 1  # 暗模块


def _is_function(size, align):
    """标记功能模块 (数据放置时跳过):
    定位图案+分隔线 / 时序行列 / 对齐图案 / format 条带+暗模块"""
    f = [[False] * size for _ in range(size)]
    # 定位图案 + 分隔线 (三个角, 各 9x9 含越界裁剪, 含 -1 偏移覆盖分隔线)
    for cx, cy in ((0, 0), (size - 7, 0), (0, size - 7)):
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                x = cx + dx
                y = cy + dy
                if 0 <= x < size and 0 <= y < size:
                    f[y][x] = True
    # 时序图案
    for i in range(size):
        f[6][i] = True
        f[i][6] = True
    # 对齐图案
    if align is not None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                f[align + dy][align + dx] = True
    # format 条带 (两份) + 暗模块
    for i in range(9):
        f[i][8] = True
        f[8][i] = True
    for i in range(8):
        f[8][size - 1 - i] = True
    for y in range(size - 7, size):
        f[y][8] = True
    f[size - 8][8] = True
    return f


def get_matrix(text):
    """把文本编码为二维码矩阵, 返回 (n, rows) 或 (None, None)"""
    if not text:
        return (None, None)
    _init_gf()
    v = 0
    for vi, (ndata, nec, _al, _rem) in enumerate(_VERSIONS):
        if len(text.encode("utf-8")) + 2 <= ndata:
            v = vi + 1
            break
    if v == 0:
        return (None, None)
    ndata, nec, align, _rem = _VERSIONS[v - 1]
    codewords = _build_data(text, ndata)
    if codewords is None:
        return (None, None)
    codewords += _rs_ec(codewords, nec)

    size = v * 4 + 17
    m = [[0] * size for _ in range(size)]
    # 时序图案先画, 定位/分隔线后画并覆盖自身区域的行 6 / 列 6
    for i in range(size):
        m[6][i] = 1 if (i % 2 == 0) else 0
        m[i][6] = m[6][i]
    _draw_finder(m, size, 0, 0)
    _draw_finder(m, size, size - 7, 0)
    _draw_finder(m, size, 0, size - 7)
    if align is not None:
        _draw_alignment(m, size, align, align)
    _draw_format(m, size, 0)

    f = _is_function(size, align)

    # 数据位放置: 自右向左双列 zigzag, 跳过列 6, mask 0 反相
    buf = codewords
    total = len(buf) * 8
    i = 0
    right = size - 1
    while right > 0:
        if right == 6:
            right -= 1
        upward = ((right + 1) & 2) == 0
        for t in range(size):
            y = (size - 1 - t) if upward else t
            for j in range(2):
                x = right - j
                if not f[y][x]:
                    b = 0
                    if i < total:
                        b = (buf[i >> 3] >> (7 - (i & 7))) & 1
                        i += 1
                    if (x + y) % 2 == 0:
                        b ^= 1
                    m[y][x] = b
        right -= 2

    # 四边补 2 模块空白, 免去绘制侧再算静区
    quiet = 2
    n = size + quiet * 2
    out = [[0] * n for _ in range(n)]
    for y in range(size):
        row = m[y]
        orow = out[y + quiet]
        for x in range(size):
            orow[x + quiet] = row[x]
    return (n, out)

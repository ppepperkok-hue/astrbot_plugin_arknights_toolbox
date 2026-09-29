"""QR Code 编码与 PNG 输出（**纯标准库**）。

## 为什么自己写

阶段二要把扫码链接渲染成图片发给用户，而本项目**运行时零第三方依赖**（`rules.md` §7）。
引入 QR 库就是新增运行时依赖；把扫码链接交给在线二维码服务则更糟——那会把**登录用
的 scanId 泄露给第三方**。所以自己实现：字节模式 + 自己封 PNG 容器（`zlib` + `struct`）。

## 范围与取舍

- **字节模式，版本 1–10，纠错等级 L/M/Q/H**。登录链接约 70 字节，版本 5 上下就够；
  版本表止于 10 而非 40，是为了让这张表**可被人工审阅**——用不到的版本不预先撑大。
- **版本/分块表是机器生成的，不是手抄的**：从一份参考实现里导出后写死在这里，
  并用测试逐个版本、逐个等级与本实现的结果交叉比对（见 `tests/test_skland_qr.py`）。
  手抄 160 行的标准表是必然出错的做法，本项目的教训是「靠眼睛盯不住」。
- **掩码罚分按 ISO/IEC 18004 §8.8.2 实现**（四条规则）。它只影响可扫性、不影响可解码性，
  但既然要做就做对：测试会用同一个神谕逐位比对矩阵。
- **PNG 用 1 位灰度**（省体积，QQ 上更快）。过滤器固定 0（无过滤），逐行按字节对齐补位。
"""

from __future__ import annotations

import struct
import zlib
from typing import Final

__all__ = ["QrError", "encode_matrix", "qr_png", "to_png"]

#: 支持到哪个版本。见模块 docstring 的取舍说明。
MAX_VERSION: Final[int] = 10

#: 纠错等级 → 格式信息里的 2 位编码（ISO/IEC 18004 表 25）。
FORMAT_LEVEL_BITS: Final[dict[str, int]] = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

#: 每版本每等级的分块表：`(块数, 总码字, 数据码字)` 三元组，可多组。
#: 机器生成（见模块 docstring），勿手改。
_BLOCK_TABLE: Final[dict[int, dict[str, tuple[tuple[int, int, int], ...]]]] = {
    1: {"L": (1, 26, 19), "M": (1, 26, 16), "Q": (1, 26, 13), "H": (1, 26, 9)},
    2: {"L": (1, 44, 34), "M": (1, 44, 28), "Q": (1, 44, 22), "H": (1, 44, 16)},
    3: {"L": (1, 70, 55), "M": (1, 70, 44), "Q": (2, 35, 17), "H": (2, 35, 13)},
    4: {"L": (1, 100, 80), "M": (2, 50, 32), "Q": (2, 50, 24), "H": (4, 25, 9)},
    5: {
        "L": (1, 134, 108),
        "M": (2, 67, 43),
        "Q": (2, 33, 15, 2, 34, 16),
        "H": (2, 33, 11, 2, 34, 12),
    },
    6: {"L": (2, 86, 68), "M": (4, 43, 27), "Q": (4, 43, 19), "H": (4, 43, 15)},
    7: {
        "L": (2, 98, 78),
        "M": (4, 49, 31),
        "Q": (2, 32, 14, 4, 33, 15),
        "H": (4, 39, 13, 1, 40, 14),
    },
    8: {
        "L": (2, 121, 97),
        "M": (2, 60, 38, 2, 61, 39),
        "Q": (4, 40, 18, 2, 41, 19),
        "H": (4, 40, 14, 2, 41, 15),
    },
    9: {
        "L": (2, 146, 116),
        "M": (3, 58, 36, 2, 59, 37),
        "Q": (4, 36, 16, 4, 37, 17),
        "H": (4, 36, 12, 4, 37, 13),
    },
    10: {
        "L": (2, 86, 68, 2, 87, 69),
        "M": (4, 69, 43, 1, 70, 44),
        "Q": (6, 43, 19, 2, 44, 20),
        "H": (6, 43, 15, 2, 44, 16),
    },
}

#: 对齐图案中心坐标（版本 1 没有；机器生成）。
_ALIGNMENT: Final[dict[int, tuple[int, ...]]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}


class QrError(ValueError):
    """输入无法编成受支持的二维码（例如内容超长）。"""


# --- GF(256) ----------------------------------------------------------------

_EXP: Final[list[int]] = [0] * 512
_LOG: Final[list[int]] = [0] * 256


def _init_galois() -> None:
    """建 GF(256) 的指数/对数表（本原多项式 0x11D）。"""
    value = 1
    for i in range(255):
        _EXP[i] = value
        _LOG[value] = i
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_galois()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator_poly(ec_count: int) -> list[int]:
    """生成多项式 ∏(x − α^i)，i ∈ [0, ec_count)。最高次在前，首项恒为 1。"""
    poly = [1]
    for i in range(ec_count):
        factor = _EXP[i]
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            if coef:
                nxt[j + 1] ^= _gf_mul(coef, factor)
        poly = nxt
    return poly


def _ec_codewords(data: list[int], ec_count: int) -> list[int]:
    """对一段数据码字求 RS 余数（LFSR 形式），返回 ec_count 个纠错码字。"""
    gen = _generator_poly(ec_count)
    remainder = [0] * ec_count
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        if factor:
            for i in range(ec_count):
                remainder[i] ^= _gf_mul(gen[i + 1], factor)
    return remainder


# --- 位流与码字 --------------------------------------------------------------


def _bits_of(value: int, length: int) -> list[int]:
    return [(value >> (length - 1 - i)) & 1 for i in range(length)]


def _codeword_from(bits: list[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value


def _data_capacity_bits(version: int, level: str) -> int:
    return sum(count * data_cw for count, _total, data_cw in _grouped(version, level)) * 8


def _grouped(version: int, level: str) -> tuple[tuple[int, int, int], ...]:
    """把扁平表项切成 `(块数, 总码字, 数据码字)` 三元组。"""
    flat = _BLOCK_TABLE[version][level]
    return tuple(flat[i : i + 3] for i in range(0, len(flat), 3))


def _choose_version(byte_length: int, level: str) -> int:
    for version in range(1, MAX_VERSION + 1):
        count_bits = 8 if version <= 9 else 16
        if 4 + count_bits + byte_length * 8 <= _data_capacity_bits(version, level):
            return version
    raise QrError(f"内容太长：{byte_length} 字节超过版本 {MAX_VERSION} 在纠错等级 {level} 下的容量")


def _encode_codewords(data: bytes, version: int, level: str) -> list[int]:
    """字节模式编码 + 补齐填充码字，返回全部**数据**码字。"""
    capacity_bits = _data_capacity_bits(version, level)
    count_bits = 8 if version <= 9 else 16

    bits = _bits_of(0b0100, 4) + _bits_of(len(data), count_bits)
    for byte in data:
        bits += _bits_of(byte, 8)

    bits += [0] * min(4, capacity_bits - len(bits))  # 终止符
    while len(bits) % 8:  # 补齐到字节边界
        bits.append(0)

    codewords = [_codeword_from(bits[i : i + 8]) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    index = 0
    while len(codewords) * 8 < capacity_bits:
        codewords.append(pad[index % 2])
        index += 1
    return codewords


def _split_blocks(codewords: list[int], version: int, level: str) -> list[list[int]]:
    blocks: list[list[int]] = []
    position = 0
    for count, _total, data_cw in _grouped(version, level):
        for _ in range(count):
            blocks.append(codewords[position : position + data_cw])
            position += data_cw
    return blocks


def _interleave(blocks: list[list[int]]) -> list[int]:
    out: list[int] = []
    for i in range(max(len(block) for block in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    return out


def _final_codewords(data: bytes, version: int, level: str) -> list[int]:
    grouped = _grouped(version, level)
    ec_count = grouped[0][1] - grouped[0][2]
    data_blocks = _split_blocks(_encode_codewords(data, version, level), version, level)
    ec_blocks = [_ec_codewords(block, ec_count) for block in data_blocks]
    return _interleave(data_blocks) + _interleave(ec_blocks)


# --- 矩阵构建 ----------------------------------------------------------------


def _draw_finder(modules: list[list[bool]], function: list[list[bool]], cx: int, cy: int) -> None:
    """画定位图案（7×7）与其外侧一圈分隔符，中心在 (cx, cy)。"""
    size = len(modules)
    for dy in range(-4, 5):
        for dx in range(-4, 5):
            x, y = cx + dx, cy + dy
            if 0 <= x < size and 0 <= y < size:
                dark = max(abs(dx), abs(dy)) not in (2, 4)
                modules[y][x] = dark
                function[y][x] = True


def _draw_alignment(
    modules: list[list[bool]], function: list[list[bool]], cx: int, cy: int
) -> None:
    """画对齐图案（5×5），中心在 (cx, cy)。"""
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            dark = max(abs(dx), abs(dy)) != 1
            modules[cy + dy][cx + dx] = dark
            function[cy + dy][cx + dx] = True


def _draw_format_bits(
    modules: list[list[bool]], function: list[list[bool]], size: int, level: str, mask: int
) -> None:
    """写格式信息（15 位，BCH(15,5)，与 0x5412 异或）。"""
    data = (FORMAT_LEVEL_BITS[level] << 3) | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    bits = ((data << 10) | remainder) ^ 0x5412

    def bit(index: int) -> bool:
        return ((bits >> index) & 1) != 0

    def put(y: int, x: int, value: bool) -> None:
        modules[y][x] = value
        function[y][x] = True

    # 第一份：沿第 8 列自下而上，再沿第 8 行向左——两段都跳过被时序图案占用的第 6 格。
    for i in range(6):
        put(i, 8, bit(i))
    put(7, 8, bit(6))
    put(8, 8, bit(7))
    put(8, 7, bit(8))
    for i in range(9, 15):
        put(8, 14 - i, bit(i))

    # 第二份：第 8 行最右 8 格 + 第 8 列最下 7 格。
    for i in range(8):
        put(8, size - 1 - i, bit(i))
    for i in range(8, 15):
        put(size - 15 + i, 8, bit(i))

    # 固定的黑模块
    put(size - 8, 8, True)


def _draw_version_bits(
    modules: list[list[bool]], function: list[list[bool]], size: int, version: int
) -> None:
    """版本 ≥ 7 才有的版本信息（18 位，BCH(18,6)）。"""
    if version < 7:
        return
    remainder = version
    for _ in range(12):
        remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
    bits = (version << 12) | remainder
    for i in range(18):
        value = ((bits >> i) & 1) != 0
        a = size - 11 + i % 3
        b = i // 3
        modules[a][b] = value
        modules[b][a] = value
        function[a][b] = True
        function[b][a] = True


_MASK_FUNCS: Final[tuple] = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: (x * y) % 2 + (x * y) % 3 == 0,
    lambda x, y: ((x * y) % 2 + (x * y) % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + (x * y) % 3) % 2 == 0,
)


def _apply_mask(modules: list[list[bool]], function: list[list[bool]], mask: int) -> None:
    predicate = _MASK_FUNCS[mask]
    for y, row in enumerate(modules):
        for x in range(len(row)):
            if not function[y][x] and predicate(x, y):
                row[x] = not row[x]


def _lines(modules: list[list[bool]]) -> list[list[bool]]:
    """按行与按列各取一遍（罚分规则 1/3 对两个方向都要算）。"""
    size = len(modules)
    return modules + [[modules[y][x] for y in range(size)] for x in range(size)]


def _penalty_runs(modules: list[list[bool]]) -> int:
    """规则 1：同色连续 ≥5，罚 `3 + (长度 − 5)`。"""
    size = len(modules)
    score = 0
    for line in _lines(modules):
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    return score


def _penalty_blocks(modules: list[list[bool]]) -> int:
    """规则 2：2×2 同色块，每块罚 3。"""
    size = len(modules)
    score = 0
    for y in range(size - 1):
        for x in range(size - 1):
            colour = modules[y][x]
            if (
                modules[y][x + 1] == colour
                and modules[y + 1][x] == colour
                and modules[y + 1][x + 1] == colour
            ):
                score += 3
    return score


def _penalty_patterns(modules: list[list[bool]]) -> int:
    """规则 3：1:1:3:1:1 且一侧紧邻 4 个浅色模块，每次罚 40。"""
    size = len(modules)
    core = (True, False, True, True, True, False, True)
    score = 0
    for line in _lines(modules):
        for start in range(size - 6):
            if tuple(line[start : start + 7]) != core:
                continue
            before = start >= 4 and not any(line[start - 4 : start])
            after = start + 11 <= size and not any(line[start + 7 : start + 11])
            if before or after:
                score += 40
    return score


def _penalty_balance(modules: list[list[bool]]) -> int:
    """规则 4：深浅比例每偏离 50% 达 5%，罚 10。"""
    size = len(modules)
    dark = sum(sum(1 for cell in row if cell) for row in modules)
    percent = dark * 100 / (size * size)
    return int(abs(percent - 50) / 5) * 10


def _penalty(modules: list[list[bool]]) -> int:
    """四条罚分规则合计（ISO/IEC 18004 §8.8.2），越低越好。"""
    return (
        _penalty_runs(modules)
        + _penalty_blocks(modules)
        + _penalty_patterns(modules)
        + _penalty_balance(modules)
    )


def encode_matrix(
    text: str, *, error_level: str = "M", mask: int | None = None
) -> list[list[bool]]:
    """把文本编成二维码矩阵（`True` 为深色），**不含静默区**。

    Args:
        mask: 固定掩码（0–7）。默认 `None` 表示按罚分自动选最优——**这是正常用法**，
            显式指定只用于测试与排障。

    Raises:
        QrError: 纠错等级非法、掩码越界，或内容超出受支持版本的容量。
    """
    if error_level not in FORMAT_LEVEL_BITS:
        raise QrError(f"未知纠错等级：{error_level!r}（可选 {'/'.join(FORMAT_LEVEL_BITS)}）")

    data = text.encode("utf-8")
    version = _choose_version(len(data), error_level)
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]

    # 时序图案先铺满整行/整列 6，定位图案随后覆盖两端（顺序不能反）。
    for i in range(size):
        modules[6][i] = i % 2 == 0
        function[6][i] = True
        modules[i][6] = i % 2 == 0
        function[i][6] = True

    _draw_finder(modules, function, 3, 3)
    _draw_finder(modules, function, size - 4, 3)
    _draw_finder(modules, function, 3, size - 4)

    centres = _ALIGNMENT[version]
    if centres:
        last = len(centres) - 1
        for i, cy in enumerate(centres):
            for j, cx in enumerate(centres):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue  # 三个角已被定位图案占用
                _draw_alignment(modules, function, cx, cy)

    _draw_version_bits(modules, function, size, version)
    _draw_format_bits(modules, function, size, error_level, 0)  # 先占位，掩码定后重画

    codewords = _final_codewords(data, version, error_level)
    stream = [bit for byte in codewords for bit in _bits_of(byte, 8)]
    total = len(stream)

    index = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vertical in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = (size - 1 - vertical) if upward else vertical
                if not function[y][x] and index < total:
                    modules[y][x] = stream[index] == 1
                    index += 1
        right -= 2

    best_mask = 0
    best_score = None
    candidates = (mask,) if mask is not None else range(8)
    for candidate in candidates:
        if not 0 <= candidate <= 7:
            raise QrError(f"掩码必须在 0–7，收到 {candidate}")
        _apply_mask(modules, function, candidate)
        _draw_format_bits(modules, function, size, error_level, candidate)
        score = _penalty(modules)
        if best_score is None or score < best_score:
            best_score, best_mask = score, candidate
        _apply_mask(modules, function, candidate)  # 异或两次即还原

    _apply_mask(modules, function, best_mask)
    _draw_format_bits(modules, function, size, error_level, best_mask)
    return modules


# --- PNG 输出 ----------------------------------------------------------------


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def to_png(matrix: list[list[bool]], *, scale: int = 8, border: int = 4) -> bytes:
    """把矩阵渲染成 1 位灰度 PNG，四周留 `border` 个模块宽的静默区。

    静默区不是装饰：**没有它扫码器可能根本认不出**，所以默认给足 4 个模块。

    Raises:
        QrError: `scale` 或 `border` 非法。
    """
    if scale < 1:
        raise QrError(f"scale 必须 ≥ 1，收到 {scale}")
    if border < 0:
        raise QrError(f"border 不能为负，收到 {border}")

    modules_wide = len(matrix)
    width = (modules_wide + border * 2) * scale
    row_bytes = (width + 7) // 8

    raw = bytearray()
    for row_index in range(width):
        raw.append(0)  # 过滤器类型 0（不过滤）
        line = bytearray(row_bytes)
        y = row_index // scale - border
        for col_index in range(width):
            x = col_index // scale - border
            dark = 0 <= x < modules_wide and 0 <= y < modules_wide and matrix[y][x]
            # 灰度 PNG 里采样值 1 是**白**、0 是**黑**，与「深色 = 1」的直觉相反。
            # 这里写反过一次，整张图反色——Pillow 逐像素比对才抓出来（见模块 docstring）。
            if not dark:
                line[col_index // 8] |= 0x80 >> (col_index % 8)
        raw += line

    header = struct.pack(">IIBBBBB", width, width, 1, 0, 0, 0, 0)  # 1 位灰度
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def qr_png(text: str, *, scale: int = 8, border: int = 4, error_level: str = "M") -> bytes:
    """一步到位：文本 → PNG 字节。"""
    return to_png(encode_matrix(text, error_level=error_level), scale=scale, border=border)

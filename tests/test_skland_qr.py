"""`modules/skland/qr.py` 的测试。

## 这些金标是怎么来的（可信度说明）

`qr.py` 是手写的二维码编码器，**正确性不能靠自己证明**。开发时用一份参考实现
（`qrcode` 库）做了逐位比对：**强制两边使用同一掩码、包含格式信息**，
61 个载荷 × 4 个纠错等级 × 8 个掩码 = **1848 项检查，0 处不符**；
PNG 侧用 Pillow 逐像素比对（这抓到过一次**整图反色**的 bug）。

比对脚本是开发期工具、**未进仓库**（`qrcode`/`Pillow` 不是本项目的依赖）。
所以这里放的是**冻结金标**：把上面已验证过的输出固化成哈希，作用是把行为钉住——
**将来任何改动导致矩阵变化都会红**。

⚠️ 金标只能防回归，**不能证明正确**。正确性的证据是上面那次比对。
"""

from __future__ import annotations

import hashlib
import struct
import zlib

import pytest

from modules.skland import qr

#: (载荷, 纠错等级, 边长, 矩阵行串的 sha256)
GOLDEN: tuple[tuple[str, str, int, str], ...] = (
    (
        "hypergryph://scan_login?scanId=0123456789abcdef",
        "M",
        33,
        "6443907184185d9f5eb4ea944231661f04db71718327aaef61498bb03c4419f2",
    ),
    (
        "hypergryph://scan_login?scanId=a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4",
        "M",
        41,
        "a43e38c2f7af830db993a9ea96848483bd8b69204d606bbd260127de97ba28d5",
    ),
    ("x", "L", 21, "f7f66f7f7d11bcdcde05680533b87731af1f9a76788e86f5bf33f9dbdf3ee71c"),
    ("A", "H", 21, "34ab4e28e91b89e3666a2b20c3e91698c6894acaa3d84a2ae8e7eed70baa3162"),
    ("hello, world", "Q", 25, "697bb18ca56d73a451391239f62e645236788a9f9deefeb9107143907b6dc536"),
    (
        "hypergryph://scan_login?scanId=ffffffffffffffffffffffffffffffffffffffff",
        "L",
        33,
        "546cf56ba8d0d07e83b3b517374bea3e97c285627fb10e8fd74695480e72d577",
    ),
    (
        "012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789z",
        "M",
        45,
        "d723e6087a333df9dbde18cbdeaba34bfb5a25fb4a1fcc2f04fb0e73c9445481",
    ),
    ("~" * 150 + "x", "L", 45, "fdf8a35a1db8e859f568b4fd703475e28d047f846131e46718077056e3f38ca3"),
)


def _rows_digest(matrix: list[list[bool]]) -> str:
    rows = "/".join("".join("1" if cell else "0" for cell in row) for row in matrix)
    return hashlib.sha256(rows.encode()).hexdigest()


@pytest.mark.parametrize(("text", "level", "size", "digest"), GOLDEN)
def test_matrix_matches_frozen_golden(text: str, level: str, size: int, digest: str) -> None:
    matrix = qr.encode_matrix(text, error_level=level)
    assert len(matrix) == size
    assert all(len(row) == size for row in matrix)
    assert _rows_digest(matrix) == digest


def test_every_level_produces_a_valid_size_for_the_same_payload() -> None:
    """纠错等级越高，同一载荷需要越大的版本——至少不能变小。"""
    sizes = [
        len(qr.encode_matrix("hypergryph://scan_login?scanId=" + "a" * 24, error_level=level))
        for level in ("L", "M", "Q", "H")
    ]
    assert sizes == sorted(sizes), sizes


def test_finder_patterns_and_timing_are_present() -> None:
    """结构抽查：三个定位图案的角与第 6 行/列的时序图案。

    这些位置**不受掩码影响**（它们是功能模块），所以可以稳定断言。
    """
    matrix = qr.encode_matrix("x", error_level="L")
    size = len(matrix)

    for corner_y, corner_x in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert matrix[corner_y][corner_x] is True  # 外圈角
        assert matrix[corner_y + 1][corner_x + 1] is False  # 内圈留白
        assert matrix[corner_y + 3][corner_x + 3] is True  # 中心

    # 时序图案：第 6 行/列在两端定位图案之间交替
    for i in range(8, size - 8):
        assert matrix[6][i] == (i % 2 == 0)
        assert matrix[i][6] == (i % 2 == 0)


def test_format_information_round_trips() -> None:
    """从矩阵里把那 15 位格式信息读回来，必须还原出纠错等级与掩码。"""
    for level in ("L", "M", "Q", "H"):
        for mask in (0, 5, 7):
            matrix = qr.encode_matrix(
                "hypergryph://scan_login?scanId=abc", error_level=level, mask=mask
            )
            bits = 0
            for i in range(6):
                bits |= (1 if matrix[i][8] else 0) << i
            bits |= (1 if matrix[7][8] else 0) << 6
            bits |= (1 if matrix[8][8] else 0) << 7
            bits |= (1 if matrix[8][7] else 0) << 8
            for i in range(9, 15):
                bits |= (1 if matrix[8][14 - i] else 0) << i
            data = (bits ^ 0x5412) >> 10
            assert data >> 3 == qr.FORMAT_LEVEL_BITS[level]
            assert data & 0x7 == mask


# --- PNG -------------------------------------------------------------------


def _decode_png(png: bytes) -> tuple[int, int, list[list[bool]]]:
    """把本模块产出的 1 位灰度 PNG 解回像素（True = 白）。

    故意**自己解**而不是用图像库：测试不许引入依赖，而且自己解才能验证
    「深色 = 黑」这个曾经写反过的地方。
    """
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    position = 8
    chunks: dict[bytes, list[bytes]] = {}
    while position < len(png):
        (length,) = struct.unpack(">I", png[position : position + 4])
        tag = png[position + 4 : position + 8]
        payload = png[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", png[position + 8 + length : position + 12 + length])
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, f"{tag!r} CRC 不符"
        chunks.setdefault(tag, []).append(payload)
        position += 12 + length

    width, height, depth, colour, _, _, _ = struct.unpack(">IIBBBBB", chunks[b"IHDR"][0])
    assert (depth, colour) == (1, 0), "应为 1 位灰度"
    raw = zlib.decompress(b"".join(chunks[b"IDAT"]))
    row_bytes = (width + 7) // 8
    rows: list[list[bool]] = []
    for y in range(height):
        line = raw[y * (row_bytes + 1) : (y + 1) * (row_bytes + 1)]
        assert line[0] == 0, "过滤器应为 0"
        rows.append([(line[1 + x // 8] >> (7 - x % 8)) & 1 == 1 for x in range(width)])
    return width, height, rows


@pytest.mark.parametrize(("scale", "border"), [(1, 0), (3, 2), (8, 4)])
def test_png_pixels_match_the_matrix(scale: int, border: int) -> None:
    matrix = qr.encode_matrix("hypergryph://scan_login?scanId=deadbeef", error_level="M")
    width, height, pixels = _decode_png(qr.to_png(matrix, scale=scale, border=border))

    expected = (len(matrix) + border * 2) * scale
    assert (width, height) == (expected, expected)

    for y in range(height):
        for x in range(width):
            module_x, module_y = x // scale - border, y // scale - border
            inside = 0 <= module_x < len(matrix) and 0 <= module_y < len(matrix)
            want_dark = inside and matrix[module_y][module_x]
            assert pixels[y][x] is not want_dark, f"({x},{y}) 明暗不符"


def test_png_has_a_quiet_zone() -> None:
    """静默区不是装饰：它全白，且四周各 `border` 个模块宽。

    注意 `_decode_png` 的约定是 `True = 白`（PNG 灰度里采样值 1 是白），
    所以「全白」在这里断言的是 `all(...)`，不是 `not any(...)`。
    """
    matrix = qr.encode_matrix("x", error_level="L")
    _, _, pixels = _decode_png(qr.to_png(matrix, scale=2, border=4))
    for i in range(8):  # 4 个模块 × 2 像素
        assert all(pixels[i])  # 顶边全白
        assert all(pixels[-(i + 1)])  # 底边全白
        assert all(row[i] for row in pixels)  # 左边
        assert all(row[-(i + 1)] for row in pixels)  # 右边


def test_qr_png_wraps_encoding() -> None:
    png = qr.qr_png("hypergryph://scan_login?scanId=abc", scale=2)
    width, _, _ = _decode_png(png)
    assert width == (len(qr.encode_matrix("hypergryph://scan_login?scanId=abc")) + 8) * 2


# --- 失败路径 ---------------------------------------------------------------


def test_rejects_unknown_error_level() -> None:
    with pytest.raises(qr.QrError, match="纠错等级"):
        qr.encode_matrix("x", error_level="X")


def test_rejects_payload_beyond_supported_version() -> None:
    with pytest.raises(qr.QrError, match="太长"):
        qr.encode_matrix("a" * 500, error_level="H")


@pytest.mark.parametrize("mask", [-1, 8])
def test_rejects_out_of_range_mask(mask: int) -> None:
    with pytest.raises(qr.QrError, match="掩码"):
        qr.encode_matrix("x", mask=mask)


def test_rejects_bad_scale_and_border() -> None:
    matrix = qr.encode_matrix("x")
    with pytest.raises(qr.QrError, match="scale"):
        qr.to_png(matrix, scale=0)
    with pytest.raises(qr.QrError, match="border"):
        qr.to_png(matrix, border=-1)


def test_all_levels_and_masks_are_usable() -> None:
    """没有任何 (等级, 掩码) 组合会崩——固定掩码是排障手段，不能是个陷阱。"""
    for level in "LMQH":
        for mask in range(8):
            matrix = qr.encode_matrix(
                "hypergryph://scan_login?scanId=abc", error_level=level, mask=mask
            )
            assert len(matrix) == len(matrix[0])

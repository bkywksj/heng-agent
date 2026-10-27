"""终端内图片缩略图：把图片编码成 Sixel，直接画在终端里（Windows Terminal 1.22 起支持）。

Sixel 的编码方式：图片按 6 像素高切成横条；每条里逐个颜色画一遍，每一列 6 个像素的有无合成一个字符
（`?` 起，加上 6 位掩码）；`$` 回到本条行首画下一个颜色，`-` 换到下一条；连续相同字符用 `!次数字符` 压缩。
颜色先用 Pillow 降到最多 64 色——缩略图够用，编码也快（360×220 约 0.1 秒级）。

只在支持的终端里画（见 supports_sixel）：不支持的终端收到这串控制序列，有的会显示成一屏乱码。
"""

from __future__ import annotations

import os
from pathlib import Path

MAX_WIDTH, MAX_HEIGHT = 360, 220
MAX_COLORS = 64


def supports_sixel(setting: str) -> bool:
    """image_preview 设置：on 总画、off 不画、auto 只在 Windows Terminal 里画（WT_SESSION 是它注入的环境变量）。"""
    if setting == "on":
        return True
    if setting == "off":
        return False
    return bool(os.environ.get("WT_SESSION"))


def _rle(column_codes: bytearray) -> str:
    """一个颜色在一条里的各列字符，连续相同的用 `!次数字符` 压缩（超过 3 个才划算）。"""
    out: list[str] = []
    i, n = 0, len(column_codes)
    while i < n:
        value = column_codes[i]
        j = i + 1
        while j < n and column_codes[j] == value:
            j += 1
        char = chr(63 + value)
        run = j - i
        out.append(f"!{run}{char}" if run > 3 else char * run)
        i = j
    return "".join(out)


def encode(path: Path, max_width: int = MAX_WIDTH, max_height: int = MAX_HEIGHT) -> str | None:
    """把图片编码成 Sixel 字符串（等比缩到不超过 max_width × max_height）；读不了的图片返回 None。"""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            img.thumbnail((max_width, max_height))
            rgb = img.convert("RGB")
    except (OSError, UnidentifiedImageError):
        return None
    indexed = rgb.quantize(colors=MAX_COLORS)
    palette = indexed.getpalette() or []
    width, height = indexed.size
    pixels = indexed.tobytes()
    used = sorted(set(pixels))

    parts = ["\x1bP0;1;0q", f'"1;1;{width};{height}']  # 1 = 没画到的像素透明；光栅属性给出实际尺寸
    for c in used:
        r, g, b = palette[c * 3 : c * 3 + 3]
        parts.append(f"#{c};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}")  # 颜色用 0-100 的百分比
    for top in range(0, height, 6):
        bands: dict[int, bytearray] = {}
        for dy in range(min(6, height - top)):
            row = (top + dy) * width
            bit = 1 << dy
            for x in range(width):
                color = pixels[row + x]
                codes = bands.get(color)
                if codes is None:
                    codes = bands[color] = bytearray(width)
                codes[x] |= bit
        parts.append("$".join(f"#{color}{_rle(codes)}" for color, codes in bands.items()))
        parts.append("-")
    parts.append("\x1b\\")
    return "".join(parts)

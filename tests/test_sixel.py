"""Sixel 缩略图：编码后用一个小解码器还原回像素，和（降色后的）原图逐像素比对。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

from heng_agent.ui.sixel import MAX_COLORS, _rle, encode, supports_sixel


def decode(seq: str) -> tuple[int, int, dict[tuple[int, int], int]]:
    """最小的 Sixel 解码器：返回 (宽, 高, {(x, y): 颜色号})。只支持 encode 用到的那几种指令。"""
    assert seq.startswith("\x1bP0;1;0q") and seq.endswith("\x1b\\")
    body = seq[len("\x1bP0;1;0q") : -2]
    m = re.match(r'"1;1;(\d+);(\d+)', body)
    assert m
    width, height = int(m.group(1)), int(m.group(2))
    body = body[m.end() :]
    pixels: dict[tuple[int, int], int] = {}
    color, x, top, i = 0, 0, 0, 0
    while i < len(body):
        ch = body[i]
        if ch == "#":
            j = i + 1
            while j < len(body) and (body[j].isdigit() or body[j] == ";"):
                j += 1
            color = int(body[i + 1 : j].split(";")[0])  # 定义颜色（#c;2;r;g;b）也会顺带选中它
            i = j
            continue
        if ch == "$":
            x = 0
        elif ch == "-":
            x, top = 0, top + 6
        elif ch == "!":
            j = i + 1
            while body[j].isdigit():
                j += 1
            count, code = int(body[i + 1 : j]), ord(body[j]) - 63
            for _ in range(count):
                for dy in range(6):
                    if code >> dy & 1:
                        pixels[(x, top + dy)] = color
                x += 1
            i = j + 1
            continue
        else:
            code = ord(ch) - 63
            for dy in range(6):
                if code >> dy & 1:
                    pixels[(x, top + dy)] = color
            x += 1
        i += 1
    return width, height, pixels


def test_rle() -> None:
    assert _rle(bytearray([0, 0, 0, 0, 5])) == "!4?D"  # 4 个以上才压缩；? = 0，D = 63+5
    assert _rle(bytearray([1, 1, 1])) == "@@@"


def test_encode_roundtrip_matches_every_pixel(tmp_path: Path) -> None:
    img = Image.new("RGB", (50, 20), (255, 255, 255))
    for x in range(50):
        for y in range(20):
            img.putpixel((x, y), (x * 5, y * 12, (x + y) * 3))  # 渐变：颜色多、横条边界不整齐（20 不是 6 的倍数）
    path = tmp_path / "grad.png"
    img.save(path)
    seq = encode(path)
    assert seq is not None
    width, height, pixels = decode(seq)
    assert (width, height) == (50, 20)
    expected = img.quantize(colors=MAX_COLORS)
    for y in range(20):
        for x in range(50):
            assert pixels.get((x, y)) == expected.getpixel((x, y)), (x, y)  # 每个像素都画了、颜色都对


def test_encode_scales_down_and_rejects_non_images(tmp_path: Path) -> None:
    big = tmp_path / "big.png"
    Image.new("RGB", (1920, 1080), (10, 20, 30)).save(big)
    seq = encode(big)
    assert seq is not None
    width, height, _ = decode(seq)
    assert width == 360 and height <= 220  # 等比缩到不超过 360×220（高按 Pillow 的取整，约 203）
    (tmp_path / "x.png").write_bytes(b"not an image")
    assert encode(tmp_path / "x.png") is None


def test_supports_sixel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WT_SESSION", raising=False)
    assert not supports_sixel("auto") and supports_sixel("on") and not supports_sixel("off")
    monkeypatch.setenv("WT_SESSION", "abc")
    assert supports_sixel("auto") and not supports_sixel("off")


def test_repl_draws_thumbnails_only_when_enabled(tmp_path: Path) -> None:
    import io

    from rich.console import Console

    from fake_model import FakeModel
    from heng_agent.agent import Agent
    from heng_agent.core.config import Settings
    from heng_agent.ui.repl import Repl

    work = tmp_path / "w"
    work.mkdir()
    Image.new("RGB", (40, 30), (200, 50, 50)).save(work / "a.png")

    def drawn(mode: str, terminal: bool = True) -> bool:
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=terminal, width=100)
        repl = Repl(
            Agent(Settings(workdir=work, home=tmp_path / "h", image_preview=mode), FakeModel([])), console=console
        )
        repl.show_thumbnails(["a.png"])
        return "\x1bP0;1;0q" in buf.getvalue()

    assert drawn("on") and not drawn("off")
    assert not drawn("on", terminal=False)  # 输出被重定向（不是终端）时不画

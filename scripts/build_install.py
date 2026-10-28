"""从 scripts/install.src.ps1（好读的中文源文件）生成仓库根目录的 install.ps1（给用户下载运行的）。

    .venv/Scripts/python.exe scripts/build_install.py

为什么要生成：install.ps1 要同时在两种方式下正常运行——
- `irm 地址 | iex`：脚本被当成一段文字执行。带 BOM 的话，BOM 会粘在第一行开头，PowerShell 报
  「The term '﻿#' is not recognized」（实测，用户第一眼看到一行红字）；
- 下载到本地运行：Windows PowerShell 5.1 把没有 BOM 的文件按系统代码页（GBK）读，UTF-8 的中文会读乱，
  还可能吞掉字符串结尾的引号、整个脚本解析失败。
两头都要，只能让脚本不依赖文件编码：不带 BOM，代码里的中文写成 $([char]0x5B89) 这样的字符编码（运行时还原成中文）。
整行注释原样保留（读乱了也不影响运行，在网页上看源码还看得懂）；代码行末尾的注释去掉。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "scripts" / "install.src.ps1"
TARGET = ROOT / "install.ps1"
HEADER = "# 本文件由 scripts/install.src.ps1 生成（scripts/build_install.py），改那个文件，别直接改这里\n"


def split_comment(line: str) -> tuple[str, str]:
    """把一行代码拆成 (代码, 行尾注释)：引号外面的第一个 # 起是注释。"""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return line[:i].rstrip(), line[i:]
    return line, ""


def convert_line(line: str) -> str:
    if line.lstrip().startswith("#") or all(ord(c) < 128 for c in line):
        return line  # 整行注释、纯 ASCII 的代码：原样
    code, _ = split_comment(line)
    out: list[str] = []
    quote = ""
    for ch in code:
        if quote:
            if ch == quote:
                quote = ""
            if ord(ch) >= 128:
                if quote != '"' and ch != quote:
                    raise ValueError(f"中文只能写在双引号字符串里（单引号里 $(...) 不会展开）：{line.strip()}")
                out.append(f"$([char]0x{ord(ch):04X})")
                continue
        elif ch in ("'", '"'):
            quote = ch
        elif ord(ch) >= 128:
            raise ValueError(f"中文只能写在双引号字符串里：{line.strip()}")
        out.append(ch)
    return "".join(out)


def build() -> str:
    lines = SOURCE.read_text(encoding="utf-8-sig").splitlines()  # 源文件带不带 BOM 都行
    return HEADER + "\r\n".join(convert_line(line) for line in lines) + "\r\n"


def main() -> int:
    text = build()
    TARGET.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))  # 不带 BOM、CRLF
    print(f"已生成 {TARGET.relative_to(ROOT)}（{len(text.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

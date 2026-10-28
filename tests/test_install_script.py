"""install.ps1 由 scripts/install.src.ps1 生成：不带 BOM、代码里没有中文（写成字符编码），两种运行方式都不出错。

带 BOM 时 `irm | iex` 第一行报「The term '﻿#' is not recognized」（实测）；不带 BOM 又有中文时，Windows PowerShell 5.1
下载到本地运行会按 GBK 读乱。所以中文写成 $([char]0x...)，见 scripts/build_install.py。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_install", ROOT / "scripts" / "build_install.py")
build_install = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(build_install)  # type: ignore[union-attr]


def test_generated_file_is_up_to_date() -> None:
    committed = (ROOT / "install.ps1").read_bytes().replace(b"\r\n", b"\n")
    expected = build_install.build().encode("utf-8").replace(b"\r\n", b"\n")
    # 对不上：改的是源文件 scripts/install.src.ps1 却没重新生成，或者直接改了 install.ps1
    assert committed == expected, "改完 scripts/install.src.ps1 后运行 scripts/build_install.py 重新生成"


def test_no_bom_and_ascii_code() -> None:
    raw = (ROOT / "install.ps1").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # 带 BOM 的话 irm | iex 第一行就报错
    code = [ln for ln in raw.decode("utf-8").splitlines() if not ln.lstrip().startswith("#")]
    assert all(ord(c) < 128 for ln in code for c in ln)  # 代码里的中文都写成了字符编码：按什么编码读都不乱


def test_convert_line() -> None:
    assert build_install.convert_line("Say \"安装\" 'Cyan'") == "Say \"$([char]0x5B89)$([char]0x88C5)\" 'Cyan'"
    assert build_install.convert_line("$x = 1  # 注释去掉") == "$x = 1"
    assert build_install.convert_line("# 整行注释原样保留") == "# 整行注释原样保留"
    with pytest.raises(ValueError, match="双引号"):
        build_install.convert_line("Say '单引号里的中文'")  # 单引号里 $(...) 不会展开

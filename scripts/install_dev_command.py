"""装一个 heng-dev 命令：在任何目录直接跑这个仓库里的开发版（改完代码立即生效，数据在 ~/.heng-agent-dev）。

正式版 heng-agent 由 `uv tool install` 装（见 CHANGELOG.md 的发布步骤），两个命令并存、互不干扰。
仓库搬了位置、或换了一台电脑，重新跑一次本脚本即可：

    .venv/Scripts/python.exe scripts/install_dev_command.py

在 uv 放命令的目录（~/.local/bin，已在 PATH 里）写两个小文件：
- heng-dev.cmd：给 cmd / PowerShell 用。按系统代码页（中文 Windows 是 GBK）写——cmd 按代码页读批处理文件，
  路径里有中文（如 AI模型）时写成 UTF-8 会乱码、找不到程序；
- heng-dev：给 Git Bash 用（它不认不带扩展名的 .cmd）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET_DIR = Path(os.environ.get("UV_TOOL_BIN_DIR") or Path.home() / ".local" / "bin")


def main() -> int:
    exe = REPO / ".venv" / ("Scripts/heng-agent.exe" if sys.platform == "win32" else "bin/heng-agent")
    if not exe.is_file():
        print(f"找不到开发版程序 {exe}：先在仓库里跑 uv sync", file=sys.stderr)
        return 1
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    shell = TARGET_DIR / "heng-dev"
    posix = exe.as_posix()
    if sys.platform == "win32" and len(posix) > 1 and posix[1] == ":":
        posix = f"/{posix[0].lower()}{posix[2:]}"  # E:/x → /e/x（Git Bash 的写法）
    shell.write_text(f'#!/bin/sh\nexec "{posix}" "$@"\n', encoding="utf-8", newline="\n")
    shell.chmod(0o755)
    written = [shell]
    if sys.platform == "win32":
        cmd = TARGET_DIR / "heng-dev.cmd"
        # cmd 按「当前控制台的代码页」读批处理文件：GBK 终端里写 UTF-8 会乱码，UTF-8 终端里写 GBK 也乱码
        # （实测：智码的终端是 65001，按 GBK 写的就报「找不到路径」），路径里有中文就依赖终端设置、不可靠。
        # 所以开头几行（纯 ASCII，在什么代码页下都读得对）先记下原代码页、切到 UTF-8，后面的路径按 UTF-8 写；
        # 跑完切回去，并把退出码原样带出来。批处理是一行行读的，切换之后的行就按 UTF-8 解。
        # newline=""：不让 Python 再把 \n 换成 \r\n（否则每行变成 \r\r\n）
        cmd.write_text(
            "@echo off\r\n"
            "setlocal\r\n"  # 下面的临时变量不漏进用户的 cmd 会话
            'for /f "tokens=2 delims=:" %%a in (\'chcp\') do set "HENG_DEV_CP=%%a"\r\n'
            "chcp 65001 >nul\r\n"
            f'"{exe}" %*\r\n'
            'set "HENG_DEV_RC=%errorlevel%"\r\n'
            "chcp %HENG_DEV_CP% >nul\r\n"
            "exit /b %HENG_DEV_RC%\r\n",
            encoding="utf-8",
            newline="",
        )
        written.append(cmd)
    for path in written:
        print(f"已写入 {path}")
    print("现在在任何目录输入 heng-dev 就是开发版；heng-agent 仍是正式版。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""附件：拖入文件、粘贴路径、Alt+V 粘贴剪贴板里的图片或文件，统一变成输入里的 `@路径`。

工具只能读工作目录里的文件，所以工作目录以外的文件（桌面截图、下载目录里的文档）先复制到
`.heng-agent/attachments/` 再引用；这个目录自带 `.gitignore`，不会被提交进仓库。
文件名里有空格的也复制一份（`@路径` 以空白分隔，带空格的路径没法引用）。

剪贴板只在 Windows 上读（用系统自带的 PowerShell，不需要额外依赖）；其他系统 Alt+V 提示不支持。
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ATTACH_DIR = Path(".heng-agent") / "attachments"
_UNSAFE = re.compile(r"[^\w.\-]+")


def attach_dir(workdir: Path) -> Path:
    folder = workdir / ATTACH_DIR
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# heng-agent 的粘贴 / 拖入附件，不进仓库\n*\n", encoding="utf-8")
    return folder


def _unique(folder: Path, name: str) -> Path:
    stem, suffix = Path(name).stem or "file", Path(name).suffix
    stem = _UNSAFE.sub("_", stem).strip("_") or "file"
    target = folder / f"{stem}{suffix}"
    n = 1
    while target.exists():
        target = folder / f"{stem}-{n}{suffix}"
        n += 1
    return target


def import_path(path: Path, workdir: Path) -> str | None:
    """把一个文件或目录变成可以 @ 的相对路径：工作目录里的直接用，以外的（或带空格的）复制进附件目录。

    目录只在工作目录内才引用（不整个复制外部目录）；不存在的返回 None。
    """
    root = workdir.resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.exists():
        return None
    inside = resolved == root or root in resolved.parents
    rel = resolved.relative_to(root).as_posix() if inside else None
    if resolved.is_dir():
        return rel if rel and " " not in rel else None
    if rel and " " not in rel:
        return rel
    target = _unique(attach_dir(workdir), resolved.name)
    shutil.copy2(resolved, target)
    return target.resolve().relative_to(root).as_posix()


def paths_in_paste(text: str) -> list[Path] | None:
    """粘贴的内容是不是「一个或几个存在的文件路径」（拖入文件就是这样）。是就返回路径，否则 None。"""
    text = text.strip()
    if not text or len(text) > 4000:
        return None
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        return None
    paths = [Path(t.strip("\"'")) for t in tokens]
    if not paths or not all(p.is_absolute() and p.exists() for p in paths):
        return None
    return paths


def paste_to_mentions(text: str, workdir: Path) -> str | None:
    """粘贴的是文件路径就换成 `@相对路径`（必要时复制进附件目录）；不是就返回 None，按普通文字粘贴。"""
    paths = paths_in_paste(text)
    if paths is None:
        return None
    mentions = [m for m in (import_path(p, workdir) for p in paths) if m]
    return " ".join(f"@{m}" for m in mentions) + " " if mentions else None


#: 消息里的绝对路径：带引号的（可以有空格）或不带引号的 Windows 盘符路径 / Unix 绝对路径。
#: 排除 `//` 开头：网址里的 `//host/x.png` 在 Windows 上会被当成网络共享路径，检查它会去连那台主机、卡好几秒。
#: 不带引号的路径遇到中文标点与 @ 就停：「看 C:\a.png，帮我分析」里逗号不是空白，不停的话会连后文一起吞掉
#: （路径本身可以含中文，只排除标点）
PATH_STOP = "\\s\"'<>|@，。；：！？、（）【】《》「」“”‘’"
#: 不带引号的绝对文件路径（以扩展名结尾）；界面里渲染超链接也用这一份，免得两边规则又对不上
ABS_PATH = rf"(?:[A-Za-z]:[\\/]|/(?!/))[^{PATH_STOP}]+\.[A-Za-z0-9]{{1,8}}"
_QUOTED_PATH = re.compile(r"\"((?:[A-Za-z]:[\\/]|/(?!/))[^\"\r\n]+)\"")
_BARE_PATH = re.compile(rf"(?<![@\w/\\:])({ABS_PATH})")


def absorb_paths(text: str, workdir: Path) -> tuple[str, list[str]]:
    """把消息里出现的、真实存在的**文件**绝对路径换成 `@相对路径`（工作目录外的先复制进附件目录）。

    发送时调用，不依赖粘贴标记：
    - 智码的终端里 Ctrl+V 粘贴截图，会把图片存成 %TEMP%\\clipboard_img_*.png 再把路径写进终端；
    - 拖入文件同样是写路径；
    - 老版 conhost 会吞掉括号粘贴标记，路径就像逐字敲进来的一样，粘贴时认不出来。
    只认存在的文件，网址、不存在的路径、已经是 @ 的都原样不动。
    """
    attached: list[str] = []

    def convert(match: re.Match[str]) -> str:
        raw = match.group(1)
        path = Path(raw)
        if not path.is_file():
            return match.group(0)
        rel = import_path(path, workdir)
        if not rel:
            return match.group(0)
        attached.append(rel)
        return f"@{rel}"

    text = _QUOTED_PATH.sub(convert, text)
    text = _BARE_PATH.sub(convert, text)
    return text, attached


# ---------- 剪贴板（Windows） ----------

_PS_IMAGE = (
    "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
    "$i = [System.Windows.Forms.Clipboard]::GetImage(); "
    "if ($i -ne $null) { $i.Save($env:HENG_PASTE_TARGET, [System.Drawing.Imaging.ImageFormat]::Png); 'ok' }"
)
_PS_FILES = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; "
    "[System.Windows.Forms.Clipboard]::GetFileDropList() | ForEach-Object { $_ }"
)
_PS_TEXT = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-Clipboard -Raw"


def _powershell(script: str, **env: str) -> tuple[str, str]:
    """跑一段 PowerShell，返回 (标准输出, 错误输出)。

    参数用环境变量传（脚本里 $env:名字）：`powershell -Command 脚本 参数` 的参数**不会进 $args**，
    而是拼到命令文字后面——曾因此保存路径为空、截图存不下来。环境变量也不怕中文与空格。
    剪贴板 API 要求 STA 线程；-NoProfile 避免用户配置拖慢、干扰输出。
    """
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script]
    proc = subprocess.run(cmd, capture_output=True, timeout=15, check=False, env={**os.environ, **env})
    return (proc.stdout.decode("utf-8", errors="replace").strip(),
            proc.stderr.decode("utf-8", errors="replace").strip())  # fmt: skip


def paste_clipboard(workdir: Path, text_fallback: bool = False) -> tuple[str, str]:
    """Alt+V / Ctrl+V：剪贴板里是图片就存成 PNG，是文件列表就逐个导入。返回 (要插入的 @文字, 给用户看的说明)。

    text_fallback：都没有时退回粘贴文字（Ctrl+V 用；Windows Terminal 里 Ctrl+V 被终端自己处理，到不了这里）。
    """
    if sys.platform != "win32":
        return "", "这个系统上暂不支持读取剪贴板里的图片（可以把图片文件拖进来，或写 @路径）"
    target = _unique(attach_dir(workdir), f"paste-{_dt.datetime.now():%Y%m%d-%H%M%S}.png")
    try:
        out, err = _powershell(_PS_IMAGE, HENG_PASTE_TARGET=str(target))
        if out == "ok" and target.exists():
            rel = target.resolve().relative_to(workdir.resolve()).as_posix()
            return f"@{rel} ", f"已粘贴图片 {rel}"
        if err:  # 不再把错误吞掉：上次就是报错被吞、只显示「没有图片」，查了半天
            return "", f"读取剪贴板里的图片失败：{err.splitlines()[0][:150]}"
        files = [Path(line) for line in _powershell(_PS_FILES)[0].splitlines() if line.strip()]
        mentions = [m for m in (import_path(p, workdir) for p in files) if m]
        if mentions:
            return " ".join(f"@{m}" for m in mentions) + " ", f"已粘贴 {len(mentions)} 个文件"
        if text_fallback:
            text = _powershell(_PS_TEXT)[0]
            converted = paste_to_mentions(text, workdir)
            return (converted, "已把粘贴的文件路径换成 @ 引用") if converted else (text, "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"读取剪贴板失败：{exc}"
    return "", "剪贴板里没有图片或文件（文字请直接 Ctrl+V）"

"""显示用的函数与常量：工具名、确认框内容、结果摘要、自动链接、文件链接（从 repl.py 拆出）。"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from rich.markup import escape

from heng_agent.attachments import ABS_PATH, PATH_STOP
from heng_agent.core.types import TurnDone
from heng_agent.tools.base import Tool
from heng_agent.ui.prompt import human_tokens

#: 别名：Claude Code 用户习惯的叫法也认
ALIASES = {"/cost": "/usage", "/todos": "/todo", "/version": "/status", "/quit": "/exit", "/bashes": "/tasks"}
RESULT_PREVIEW_LINES = 3  # 命令输出、出错等只留前几行：工具过程要紧凑
#: 用户提问左侧的竖条（每行一根，像引用块）。以前提问是 ❯、回复是 ✦，两个小图标在长对话里不好分（用户反馈），
#: 改成提问一根青色竖条、回复不加图标、每问结尾一行灰字写时间与用时
PROMPT_BAR = "▌"
MIN_THINKING_BUDGET = 1024  # Anthropic 要求的思考预算下限
MAX_THUMBNAILS = 3
#: 工具调用显示成好读的动词（MCP 工具见 tool_label）
TOOL_LABELS = {
    "read_file": "读取", "glob": "找文件", "grep": "搜索", "write_file": "写入", "edit_file": "修改",
    "bash": "运行", "todo": "任务清单", "web_fetch": "读网页", "skill": "用技能", "task": "派子任务",
    "bash_output": "看输出", "kill_task": "停任务", "tool_search": "找工具", "exit_plan_mode": "交出计划",
}  # fmt: skip


def tool_label(name: str) -> str:
    """工具显示名：内置工具用动词；MCP 工具 mcp__sigil__list_credentials 显示成 sigil · list_credentials。"""
    if name in TOOL_LABELS:
        return TOOL_LABELS[name]
    if name.startswith("mcp__") and name.count("__") >= 2:
        _, server, rest = name.split("__", 2)
        return f"{server} · {rest}"
    return name


def approval_body(tool: Tool, args: dict[str, Any]) -> str:
    """确认框里的文字（edit_file / write_file 之外）：先一句工具的用途，再这次的参数。

    不能只放参数摘要：没有参数的工具（如 MCP 的「列出全部」）摘要是空的，会画出一个空框，看着像多了个输入框。
    """
    purpose = next((ln.strip() for ln in tool.description.splitlines() if ln.strip()), "")[:200]
    if not args:
        params = "（无参数）"
    elif tool.kind == "mcp":  # MCP 参数常是嵌套结构，按 JSON 排版才看得清
        params = json.dumps(args, ensure_ascii=False, indent=2)[:3000]
    else:
        params = tool.summary(args) or json.dumps(args, ensure_ascii=False)[:3000]
    if tool.name in ("bash", "web_fetch"):  # 内置工具大家都知道用途，只看要跑什么
        return params
    return f"{purpose}\n\n{params}" if purpose else params


def brief_result(name: str, content: str, is_error: bool) -> list[str]:
    """工具结果的简短显示：按工具展示最有用的那部分。出错与命令输出保留前几行，其余给个数量。"""
    lines = content.splitlines() or [""]
    counted = [line for line in lines if line.strip()]

    def preview(limit: int = RESULT_PREVIEW_LINES) -> list[str]:
        shown = [line[:160] for line in lines[:limit]]
        return shown + ([f"…（共 {len(lines)} 行）"] if len(lines) > limit else [])

    if is_error:
        return preview()
    first = lines[0].strip()
    if name == "read_file":
        return [first] if first == "（空文件）" else [f"{len(counted)} 行"]
    if name in ("glob", "grep"):
        if first.startswith(("没有", "没找到", "未找到")):
            return [first]
        return [f"{len(counted)} 个" if name == "glob" else f"{len(counted)} 处匹配"]
    if name == "web_fetch":
        return [f"读到 {len(content)} 字"]
    if name == "todo":
        return preview(limit=20)  # 任务清单整个显示
    if name in ("write_file", "edit_file", "skill"):
        return [first[:160]]
    return preview()  # bash、task、MCP 工具：输出内容本身有用，留前几行


def edit_in_editor(text: str) -> str | None:
    """Ctrl+G：把输入框里的字放进临时文件，用 $EDITOR（没设就是记事本 / vi）打开；关掉后读回来。

    没改返回 None。编辑器需要「关掉才返回」：VS Code 要写成 `code --wait`。
    """
    import shlex
    import subprocess
    import tempfile

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = Path(fh.name)
    try:
        subprocess.run([*shlex.split(editor, posix=os.name != "nt"), str(path)], check=False)
        edited = path.read_text(encoding="utf-8-sig").rstrip("\n")  # 记事本可能带 BOM、末尾加换行
    except OSError:
        return None
    finally:
        path.unlink(missing_ok=True)
    return None if edited == text else edited


_PLAIN = object()  # _choose 的返回值：选择器用不了，调用方退回打字


def _stdin_is_terminal() -> bool:
    """标准输入是不是终端（脚本、管道、CI、测试里不是）：不是就用不了方向键选择器。"""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):  # 被替换成别的对象、或已关闭
        return False


_FENCE = re.compile(r"^\s*(```|~~~)", re.MULTILINE)
_URL = re.compile(r"(?<![<(\[\w])(https?://[^\s<>()（）\[\]【】，。；：！？、\"'`]+)")


def split_done(text: str) -> tuple[str, str]:
    """流式回复里已经写完的部分与还在写的部分：在最后一个空行处切开，但不能切在代码块中间
    （前半部分的 ``` 必须成对）。没有可切的返回 ("", 原文)。"""
    idx = text.rfind("\n\n")
    while idx != -1:
        head = text[:idx]
        if len(_FENCE.findall(head)) % 2 == 0:
            return head, text[idx + 2 :]
        idx = text.rfind("\n\n", 0, idx)
    return "", text


def autolink(text: str) -> str:
    """把回复里的裸网址变成 Markdown 自动链接 <网址>，渲染出来是超链接（Ctrl+点击打开）。

    代码块与行内代码里的不动（那是代码的一部分）；已经是 [文字](网址) 或 <网址> 的不动；
    网址后面紧跟的中文标点不算进网址。
    """
    out, in_fence = [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = line.split("`")
        for i in range(0, len(parts), 2):  # 偶数段在行内代码之外
            parts[i] = _URL.sub(_wrap_url, parts[i])
        out.append("`".join(parts))
    return "\n".join(out)


def _wrap_url(match: re.Match[str]) -> str:
    """网址包成 <网址>；末尾的英文句号、逗号等通常是句子的标点，留在外面。"""
    url = match.group(1)
    core = url.rstrip(".,;:!?")
    return f"<{core}>{url[len(core) :]}"


_MENTION_OR_PATH = re.compile(rf"@([^{PATH_STOP}]+)|({ABS_PATH})")  # 与 attachments 共用路径规则


def file_link(path_text: str, workdir: Path) -> str:
    """rich 标记：指向真实文件就渲染成超链接（OSC 8），否则原样（转义后）。

    智码终端会读链接背后的本地路径、悬停弹出图片预览；Windows Terminal 里 Ctrl+点击用默认程序打开。
    不支持超链接的终端 rich 会自动只显示文字。
    """
    raw = Path(path_text)
    target = raw if raw.is_absolute() else workdir / raw
    try:
        exists = target.is_file()
    except OSError:
        exists = False
    if not exists:
        return escape(path_text)
    return f"[link={target.resolve().as_uri()}][underline]{escape(path_text)}[/underline][/link]"


def linkify(text: str, workdir: Path) -> str:
    """一行文字里的 @引用 与绝对路径（指向真实文件的）渲染成超链接，其余转义。"""
    out, last = [], 0
    for m in _MENTION_OR_PATH.finditer(text):
        out.append(escape(text[last : m.start()]))
        if m.group(1):  # @引用：@ 留在链接外
            out.append("@" + file_link(m.group(1), workdir))
        else:
            out.append(file_link(m.group(2), workdir))
        last = m.end()
    out.append(escape(text[last:]))
    return "".join(out)


def turn_summary(event: TurnDone, started: float) -> str:
    """每问结尾的一行灰字（rich 标记）：几点提的 · 用了多久 · 几轮 · token。

    以前只写「（1 轮，输入 5682 / 输出 96 token）」：没有时间，长对话里分不清哪一问是什么时候的（用户反馈）。
    """
    u = event.usage
    spent = max(time.time() - started, 0.0)
    took = f"{spent:.1f} 秒" if spent < 60 else f"{int(spent // 60)} 分 {int(spent % 60)} 秒"
    tokens = f"输入 {human_tokens(u.get('input_tokens', 0))} / 输出 {human_tokens(u.get('output_tokens', 0))}"
    when = _dt.datetime.fromtimestamp(started).strftime("%H:%M")
    return f"  [dim]{when} · 用时 {took} · {event.rounds} 轮 · {tokens}[/]"


def elapsed_text(seconds: float) -> str:
    """时长写成 8s、1m 2s、1h 3m（等待动画、工具用时用）。"""
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


class Ticker:
    """等待动画里的文字：每次重画时现算已经等了多久，括号里的时间一直在走。

    以前只写「思考中…」「运行…」：等了一两分钟屏幕上看不出来，分不清是卡住了还是在干活（用户对照 Claude Code
    的「Running… (1m 2s · timeout 10m)」提的）。rich 的等待动画每秒重画十几次，每次都会调 __rich__。
    """

    def __init__(self, text: str, hint: str, started: float, limit: float | None = None) -> None:
        self.text, self.hint, self.started, self.limit = text, hint, started, limit

    def __rich__(self) -> Any:
        from rich.text import Text

        timing = elapsed_text(time.time() - self.started)
        if self.limit:
            timing += f" · 超时 {elapsed_text(self.limit)}"
        hint = f" · {escape(self.hint)}" if self.hint else ""
        return Text.from_markup(f"[cyan]{escape(self.text)}[/] [dim]({timing}){hint}[/]")

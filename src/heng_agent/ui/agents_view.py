"""/agents 的显示：先一行一个角色（方向键选），选中再看完整说明。

以前把每个角色的完整描述、工具、路径、被忽略的工具全部平铺出来：英文长描述一折就是三四行，
五个角色占满一屏，名字淹没在说明里（用户反馈「有点乱」）。现在和 /mcp 一样：总览只给一行，详情按需看。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.markup import escape
from rich.padding import Padding

from heng_agent.agents import AgentDef
from heng_agent.core.compat import claude_home

if TYPE_CHECKING:
    from heng_agent.ui.repl import Repl

HINT = "/agents 名字 看详情 · /agents new 名字 新建"


def source(role: AgentDef, repl: Repl) -> str:
    """角色从哪来：项目 / 用户，Claude Code 的再标上「Claude」。"""
    path = role.path.resolve()
    work = repl.agent.settings.workdir.resolve()
    project = path.is_relative_to(work)
    from_claude = ".claude" in path.parts or path.is_relative_to(claude_home().resolve())
    return ("Claude " if from_claude else "") + ("项目" if project else "用户")


def short_path(path: Path) -> str:
    """用户目录缩写成 ~，路径短一截。"""
    home = Path.home()
    return f"~/{path.relative_to(home).as_posix()}" if path.is_relative_to(home) else str(path)


def first_sentence(text: str) -> str:
    """描述的第一句（中文句号、英文句点加空格、换行处断开）：总览一行放不下整段。"""
    text = " ".join(text.split())
    match = re.search(r"[。！？]|[.!?](\s|$)", text)
    return text[: match.end()].strip() if match else text


def clip(text: str, cells: int) -> str:
    """按显示宽度截断（一个汉字占两格），超出的用 … 收尾。"""
    if cell_len(text) <= cells:
        return text
    out = ""
    for ch in text:
        if cell_len(out + ch) > cells - 1:
            break
        out += ch
    return out + "…"


def pad(text: str, cells: int) -> str:
    """按显示宽度补空格对齐（中文名字也能对齐成一列）。"""
    return text + " " * max(0, cells - cell_len(text))


def headline(roles: list[AgentDef]) -> str:
    read_only = sum(1 for r in roles if r.read_only)
    return (f"[bold]子智能体角色[/]  [dim]{len(roles)} 个（只读 {read_only} · 会改动 {len(roles) - read_only}）· "
            "模型用 task 工具时按用途挑[/]")  # fmt: skip


def option_label(role: AgentDef, width: int) -> str:
    return pad(role.name, width)


def option_detail(role: AgentDef, repl: Repl, budget: int) -> str:
    """名字后面的灰字：只读 / 会改动 · 来源 · 第一句说明（截到一行内），有被忽略的工具再标个数。"""
    kind = "只读  " if role.read_only else "会改动"
    # 不用 ⚠：Windows Terminal 把它画成彩色 emoji，宽度不对，会吃掉后面的空格（实测）
    tail = f" · 忽略 {len(role.unknown_tools)} 个工具" if role.unknown_tools else ""
    head = f"{kind} · {pad(source(role, repl), 9)} · "
    room = budget - cell_len(head) - cell_len(tail)
    return head + clip(first_sentence(role.description), max(10, room)) + tail


def options_budget(repl: Repl, width: int) -> int:
    """灰字能用的宽度：终端宽减去选择器的光标、名字列与间隔，保证每个角色只占一行。"""
    return max(30, repl.console.width - width - 8)


def show_summary(repl: Repl, roles: list[AgentDef], *, with_headline: bool = True) -> None:
    """纯文字总览：选择器用不了时（管道、测试）用它，同样一行一个。"""
    c = repl.console
    if with_headline:  # 选择器刚打过标题、又用不了时只补列表，不重复
        c.print(headline(roles))
    width = max(cell_len(r.name) for r in roles)
    budget = options_budget(repl, width)
    for role in roles:
        color = "green" if role.read_only else "yellow"
        detail = escape(option_detail(role, repl, budget))
        c.print(f"  [bold {color}]{escape(option_label(role, width))}[/]  [dim]{detail}[/]", no_wrap=True)
    c.print(f"[dim]{HINT}[/]")


def show_role(repl: Repl, role: AgentDef) -> None:
    """一个角色的完整说明：描述整段（缩进排版）、工具、被忽略的工具、文件位置。"""
    c = repl.console
    kind = "[green]只读[/]（可以并行）" if role.read_only else "[yellow]会改动[/]（改文件、跑命令前向你确认）"
    c.print(
        f"[bold cyan]{escape(role.name)}[/]  {kind}  [dim]{source(role, repl)} · {escape(short_path(role.path))}[/]"
    )
    c.print(Padding(escape(" ".join(role.description.split())), (0, 0, 0, 2)))
    c.print(f"  [dim]工具[/]  {escape(', '.join(role.tools))}")
    if role.unknown_tools:
        c.print(f"  [dim]忽略[/]  [yellow]{escape(', '.join(role.unknown_tools))}[/]  [dim]不认识或不允许的工具[/]")

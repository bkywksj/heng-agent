"""/mcp：先看服务、再看工具、最后看单个工具的详情（逐级深入，参照 Claude Code / Gemini CLI）。

以前 /mcp 一口气列出全部工具：接了几个大服务就是几百行，描述里的换行还会把排版挤乱。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from rich.table import Table
from rich.text import Text

from heng_agent.mcp import CONNECTING, MCPTool

if TYPE_CHECKING:
    from heng_agent.ui.repl import Repl


@dataclass
class ServerInfo:
    name: str
    ok: bool
    detail: str = ""  # 连不上的原因
    transport: str = ""
    source: str = ""  # user / project
    tools: list[MCPTool] = field(default_factory=list)
    disabled: bool = False  # 本会话里被 /mcp disable 停用了（不是连不上）


def servers(repl: Repl) -> list[ServerInfo]:
    """按服务归拢：工具从已接入的 MCP 工具里按所属服务分组，连不上的从启动时记下的状态里取原因。"""
    found: dict[str, ServerInfo] = {}
    for tool in repl.agent.tools.values():
        if isinstance(tool, MCPTool):
            cfg = tool.client.config
            info = found.setdefault(cfg.name, ServerInfo(cfg.name, True, transport=cfg.transport, source=cfg.source))
            info.tools.append(tool)
    for name, ok, detail in repl.mcp_status:
        if not ok and name not in found:
            found[name] = ServerInfo(name, False, detail, disabled=name in repl.mcp_disabled)
    return sorted(found.values(), key=lambda s: (not s.ok, s.name))


def short_name(tool: MCPTool) -> str:
    return tool.remote_name


def summary_line(tool: MCPTool) -> str:
    """工具说明的第一句：去掉开头的 [MCP 服务名]，只取第一行（说明里常有换行，整段打出来会把排版挤乱）。"""
    text = tool.description
    prefix = f"[MCP {tool.client.config.name}]"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _one_line(text: Text, width: int) -> Text:
    text.truncate(max(20, width), overflow="ellipsis")
    return text


def headline(repl: Repl, infos: list[ServerInfo]) -> str:
    """汇总一行（rich 标记）：几个服务、共多少工具、几个连不上、是否按需加载。"""
    a = repl.agent
    total = sum(len(s.tools) for s in infos)
    failed = sum(1 for s in infos if not s.ok and not s.disabled)
    parts = [f"[bold]MCP 服务[/]  [dim]{len(infos)} 个，共 {total} 个工具[/]"]
    if failed:
        parts.append(f"[red]{failed} 个连不上[/]")
    if a.defer_mcp():
        loaded = len([n for n in a.loaded_mcp if n in a.tools])
        parts.append(f"[dim]工具多，按需加载（本会话已加载 {loaded} 个）[/]")
    return "  ·  ".join(parts)


def option_label(info: ServerInfo, width: int) -> str:
    """选择器里的一行：状态图标 + 名字（补齐宽度，后面的说明对齐成一列）。"""
    mark = "●" if info.ok else ("○" if info.disabled else "✗")
    return f"{mark} {info.name:<{width}}"


def option_detail(info: ServerInfo) -> str:
    """选择器里名字后面的灰字：连接方式、工具数、来源；连不上的写原因。"""
    if info.disabled:
        return "已停用（本会话）"
    if not info.ok:
        reason = info.detail.splitlines()[0] if info.detail else ""
        return f"连接失败：{reason[:60]}"
    source = "项目配置" if info.source == "project" else "用户配置"
    return f"{info.transport:<5} · {len(info.tools):>3} 个工具 · {source}"


def show_summary(repl: Repl) -> list[ServerInfo]:
    """纯文字的服务总览：选择器用不了时（管道、测试）用它代替。"""
    c = repl.console
    infos = servers(repl)
    if not infos:
        c.print("没有接入 MCP 服务。在 config.json 的 mcp_servers 里配置（见 README）。")
        return []
    c.print(headline(repl, infos))
    for s in infos:
        if s.ok:
            source = "项目配置" if s.source == "project" else "用户配置"
            row = Text.assemble(("  ● ", "green"), (f"{s.name:<18}", "bold"), (f"{s.transport:<6}", "cyan"),
                                (f"{len(s.tools):>4} 个工具  ", ""), (source, "grey50"))  # fmt: skip
        elif s.disabled:
            row = Text.assemble(("  ○ ", "grey50"), (f"{s.name:<18}", "grey50"), ("已停用（本会话）", "grey50"))
        else:
            row = Text.assemble(("  ✗ ", "red"), (f"{s.name:<18}", "bold"), ("连接失败：", "red"), (s.detail, "grey50"))
        c.print(_one_line(row, c.width - 2), no_wrap=True)
    return infos


def counts(repl: Repl) -> tuple[int, int, int]:
    """(连上的服务数, 它们的工具数, 连不上的服务数)：按启动时记下的连接状态算（欢迎页、/status 共用），
    连上了但一个工具也没有的也算进去；本会话停用的不算连不上。"""
    ok = [detail for _, good, detail in repl.mcp_status if good]
    tools = sum(int(m.group(1)) for d in ok if (m := re.match(r"(\d+) tools", d)))
    failed = sum(
        1
        for name, good, detail in repl.mcp_status
        if not good and name not in repl.mcp_disabled and detail != CONNECTING  # 后台还在连的不算连不上
    )
    return len(ok), tools, failed


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def toolbar_part(repl: Repl) -> tuple[str, str] | None:
    """状态栏里的 MCP 一段：(样式, 文字)；没配 MCP 返回 None。

    以前后台连接时欢迎页只写「后台连接中」，连好了也要等用户发出下一句才打印结果，看着像一直没连上。
    状态栏按管理器的实时状态显示（后台连完时界面会被叫醒重画，见 Repl._refresh_input）。
    """
    live: dict[str, tuple[bool, str]] = {}
    for manager in repl.mcp_managers:
        if manager.ready:  # 连完了才读它的状态，不和连接线程抢着读写
            live.update({name: (good, detail) for name, good, detail in manager.status})
    entries = [(name, *live.get(name, (good, detail))) for name, good, detail in repl.mcp_status]
    entries = [e for e in entries if e[0] not in repl.mcp_disabled]
    if not entries:
        return None
    waiting = sum(1 for _, good, detail in entries if not good and detail == CONNECTING)
    ok = sum(1 for _, good, _ in entries if good)
    # 只放「图标 MCP 已连上的数量」，不写中文说明：连接中、连好、连不上三种宽度一样，状态栏不跟着抖（用户要求）。
    # 橙 = 还在连（图标转圈），绿 = 都连上了，红 = 有连不上的（/mcp 看原因）
    if waiting:
        # 还在连的管理器：结果没登记，用它实时数的「已连上几个」（每连上一个加一，不是 0 直接跳到全部）
        live_ok = sum(min(m.connected, len(m.configs)) for m in repl.mcp_managers if not m.ready)
        frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]  # 连接中由 Repl.watch_mcp 定时重画，转起来
        return "class:bar.mcp-wait", f"  {frame} MCP {ok + live_ok}"
    if ok < len(entries):
        return "class:bar.mcp-fail", f"  ✗ MCP {ok}"
    return "class:bar.mcp", f"  ✓ MCP {ok}"


def connecting(repl: Repl) -> list[str]:
    """还在后台连接的服务名。"""
    return [name for name, good, detail in repl.mcp_status if not good and detail == CONNECTING]


def startup_line(repl: Repl) -> list[str]:
    """MCP 连接结果（rich 标记）：每个服务一行（连上的绿字写工具数与连接方式，连不上的红字写原因），最后一行汇总与用时。

    以前都连上时只打一行汇总，用户反馈看不出连了哪些服务、各有几个工具，改回逐个列出。
    """
    if not repl.mcp_status:
        return []
    pending = connecting(repl)
    if pending:  # 后台连接：欢迎页不等它们；连完后（输入框出现前或提交输入时）再打印这一段
        return [f"[dim]MCP：{len(pending)} 个服务正在后台连接，可以先输入（发出时还没连好会等一下）[/]"]
    lines = []
    for name, good, detail in repl.mcp_status:
        if name in repl.mcp_disabled:
            continue
        if good:
            lines.append(f'[green]MCP 服务 "{escape(name)}" 已连接 · {escape(detail)}[/]')
        else:
            lines.append(f'[red]MCP 服务 "{escape(name)}" 连接失败：{escape(detail)}[/]')
    ok, tools, failed = counts(repl)
    took = max((m.elapsed for m in repl.mcp_managers if m.background and m.elapsed is not None), default=None)
    spent = f"，用时 {took:.1f} 秒" if took is not None else ""
    broken = f"，{failed} 个连不上" if failed else ""
    lines.append(f"[dim]MCP：{ok} 个服务已连接 · {tools} 个工具{broken}{spent}  /mcp 查看[/]")
    return lines


def show_server(repl: Repl, name: str) -> bool:
    c, a = repl.console, repl.agent
    info = next((s for s in servers(repl) if s.name == name), None)
    if info is None:
        c.print(f"没有叫 {escape(name)} 的 MCP 服务。")
        return False
    if not info.ok:
        if info.disabled:
            c.print(f"[dim]○ {escape(name)} 本会话已停用；/mcp enable {escape(name)} 重新启用[/]")
        else:
            c.print(
                f"[red]✗ {escape(name)} 连接失败：{escape(info.detail)}[/]  [dim]/mcp reconnect {escape(name)} 重试[/]"
            )
        return True
    loaded = set(a.loaded_mcp)
    c.print(f"[bold]{escape(name)}[/]  [dim]{info.transport} · {len(info.tools)} 个工具 · ✓ = 本会话已加载[/]")
    width = max(len(short_name(t)) for t in info.tools) if info.tools else 10
    width = min(width, 36)
    for tool in info.tools:
        mark = "✓ " if tool.name in loaded else "  "
        row = Text.assemble(
            (f"  {mark}", "green"), (f"{short_name(tool):<{width}}  ", "cyan"), (summary_line(tool), "grey62")
        )
        c.print(_one_line(row, c.width - 2), no_wrap=True)
    c.print(f"[dim]/mcp {escape(name)} 工具名 看完整说明与参数[/]")
    return True


def show_tool(repl: Repl, server: str, tool_name: str) -> None:
    c = repl.console
    info = next((s for s in servers(repl) if s.name == server), None)
    tool = next((t for t in info.tools if short_name(t) == tool_name), None) if info else None
    if tool is None:
        c.print(f"没有这个工具：{escape(server)} · {escape(tool_name)}")
        return
    c.print(f"[bold cyan]{escape(server)} · {escape(tool_name)}[/]  [dim]（模型里叫 {escape(tool.name)}）[/]")
    prefix = f"[MCP {server}]"
    body = tool.description[len(prefix) :].strip() if tool.description.startswith(prefix) else tool.description
    c.print(escape(body))
    schema: dict[str, Any] = tool.input_schema or {}
    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not props:
        c.print("[dim]（无参数）[/]")
        return
    c.print("[bold]参数[/]")
    # 两栏排版：参数说明常常很长，换行后要和说明那一栏对齐，不能折回最左边和参数名搅在一起
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)
    for key, spec in props.items():
        kind = spec.get("type", "") if isinstance(spec, dict) else ""
        desc = (spec.get("description", "") if isinstance(spec, dict) else "").strip()
        flag = "[red]必填[/]" if key in required else "[dim]可选[/]"
        grid.add_row(f"  [cyan]{escape(key)}[/] [dim]{escape(str(kind))}[/] {flag}", escape(desc))
    c.print(grid)


ACTIONS = {"reconnect": "重新连接一个服务", "disable": "本会话停用一个服务", "enable": "重新启用停用的服务"}


def status_label(s: ServerInfo) -> str:
    return f"{len(s.tools)} 个工具" if s.ok else ("已停用" if s.disabled else "连接失败")


def arg_hints(repl: Repl, prior: list[str]) -> list[tuple[str, str, bool]]:
    """/mcp 的参数补全：操作（reconnect / disable / enable）与服务名；服务后面再补它的工具名。"""
    infos = servers(repl)
    if not prior:
        actions = [(a, desc, True) for a, desc in ACTIONS.items()]
        return actions + [(s.name, status_label(s), False) for s in infos]
    if len(prior) == 1 and prior[0] in ACTIONS:
        wanted = {"enable": lambda s: s.disabled, "disable": lambda s: s.ok}.get(prior[0], lambda s: True)
        return [(s.name, status_label(s), False) for s in infos if wanted(s)]
    if len(prior) == 1:
        info = next((s for s in infos if s.name == prior[0]), None)
        return [(short_name(t), summary_line(t)[:60], False) for t in info.tools] if info else []
    return []

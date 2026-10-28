"""查看各种状态的命令：/status（一屏看全当前状态）、/usage（本会话用量，/cost 同）、/stats（跨会话历史统计）、
/hooks（生效的钩子）。

以前这些信息散在状态栏、/doctor、/config、/cost 里，想知道「现在到底是什么状态、花了多少」要翻好几处。
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from rich.table import Table

from heng_agent.context import estimate_tokens, load_memories
from heng_agent.core.version import version_label
from heng_agent.model.providers import Profile, load_profiles
from heng_agent.model.thinking import describe as describe_thinking
from heng_agent.ui import mcp_view
from heng_agent.ui.prompt import MODE_LABEL, human_tokens

if TYPE_CHECKING:
    from heng_agent.ui.repl import Repl

USAGE_IN = ("input_tokens", "cache_read_tokens", "cache_write_tokens")
UNKNOWN_MODEL = "未知模型"


def _tokens_in(usage: dict[str, int]) -> int:
    return sum(int(usage.get(k, 0)) for k in USAGE_IN)


def _git_branch(workdir: Path) -> str:
    try:
        done = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workdir, capture_output=True,
                              text=True, timeout=3, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _money(amounts: dict[str, float]) -> str:
    return " + ".join(f"{v:.4f} {cur}" for cur, v in amounts.items() if v) or ""


# ---------- /status ----------


def show_status(repl: Repl) -> None:
    """一屏看全当前状态：版本、服务商与模型、会话、目录、权限与思考、MCP、说明文件、技能等。"""
    c, a = repl.console, repl.agent
    profile: Any = getattr(a.client, "profile", None)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()

    def row(label: str, value: str) -> None:
        grid.add_row(label, value)

    row("版本", f"heng-agent {version_label()}")
    if profile is not None:
        row("模型", f"{escape(repl.provider_name or '')} · {escape(profile.model)} · {escape(profile.base_url)}"
                    f" · 上下文 {human_tokens(a.context_window)}")  # fmt: skip
    session = a.session
    if session is not None:
        title = f"「{escape(session.title)}」" if session.title else ""
        row("会话", f"{escape(session.id)}{title} · {len(a.messages)} 条消息 · [dim]{escape(str(session.path))}[/]")
    branch = _git_branch(a.settings.workdir)
    row("目录", escape(str(a.settings.workdir)) + (f"  [cyan]git {escape(branch)}[/]" if branch else ""))
    thinking = describe_thinking(a.model_name, a.thinking, a.effort)
    row("权限", f"{escape(MODE_LABEL.get(a.permissions.mode, a.permissions.mode))} · 思考 {thinking}")
    used = a.last_input_tokens
    pct = used / a.context_window * 100 if a.context_window else 0
    row("上下文", f"{human_tokens(used)} / {human_tokens(a.context_window)}（{pct:.0f}%），"
                  f"超过 {a.settings.compact_ratio:.0%} 自动压缩")  # fmt: skip
    if repl.mcp_status:
        ok, tools, failed = mcp_view.counts(repl)
        row("MCP", f"{ok} 个服务 · {tools} 个工具" + (f" · [red]{failed} 个连不上[/]" if failed else ""))
    memories = load_memories(a.settings)
    if memories:
        total = sum(estimate_tokens(block) for _, block in memories)
        row("说明文件", f"{len(memories)} 份 · 约 {human_tokens(total)} token（/memory 看明细）")
    claude_hooks = sum(1 for h in a.hooks.hooks if h.style == "claude")
    row("扩展", f"技能 {len(a.skills)} · 子智能体角色 {len(a.agent_defs)} · 钩子 {len(a.hooks.hooks)}"
                f"（其中 Claude {claude_hooks}）· 后台任务 {len(a.ctx.tasks.running())}")  # fmt: skip
    c.print(grid)


# ---------- /usage ----------


def show_usage(repl: Repl) -> None:
    """本会话用量：输入（缓存命中率）、输出、金额；换过模型时按模型分开列；上下文占用。"""
    c, a = repl.console, repl.agent
    u = a.usage
    total_in, hit, wrote = _tokens_in(u), u.get("cache_read_tokens", 0), u.get("cache_write_tokens", 0)
    line = f"[bold]本会话[/]  输入 {human_tokens(total_in)} · 输出 {human_tokens(u.get('output_tokens', 0))} token"
    if hit or wrote:
        rate = hit / total_in * 100 if total_in else 0
        line += f"  [dim]（缓存命中 {human_tokens(hit)}、写入 {human_tokens(wrote)}，命中率 {rate:.0f}%）[/]"
    c.print(line)
    profiles = _profiles(repl)
    by_model = a.usage_by_model or ({a.model_name: u} if total_in else {})
    costs: dict[str, float] = {}
    unpriced = False
    for model, usage in by_model.items():
        profile = _price_profile(profiles, model)
        cost = profile.cost(usage) if profile is not None else None
        if cost is None:
            unpriced = True
        else:
            costs[profile.currency] = costs.get(profile.currency, 0) + cost  # type: ignore[union-attr]
        if len(by_model) > 1:
            money = f" · 约 {cost:.4f} {profile.currency}" if cost is not None and profile else ""  # type: ignore[union-attr]
            c.print(f"  [cyan]{escape(model)}[/]  输入 {human_tokens(_tokens_in(usage))} · "
                    f"输出 {human_tokens(usage.get('output_tokens', 0))}{money}")  # fmt: skip
    if costs:
        c.print(f"  约 {_money(costs)}  [dim]（按配置档价格估算，缓存读按 0.1 倍、写按 1.25 倍）[/]")
    elif unpriced or _price_profile(profiles, a.model_name) is None:  # 当前模型没配价格：告诉用户怎么配
        c.print("  [dim]配置档里填 price_in / price_out 就能按金额显示[/]")
    pct = a.last_input_tokens / a.context_window * 100 if a.context_window else 0
    c.print(f"  上下文 {pct:.0f}%（{human_tokens(a.last_input_tokens)} / {human_tokens(a.context_window)}）"
            "  [dim]/stats 看历史统计，/context 看上下文被什么占着[/]")  # fmt: skip


def _profiles(repl: Repl) -> list[Profile]:
    """算钱用的配置档：当前客户端的排第一（它的价格最准：可能是临时指定的、不在配置文件里），
    再是配置文件里的——旧会话、换过的模型按模型名在这里找。配置坏了不该拦住看用量。"""
    current = getattr(repl.agent.client, "profile", None)
    found = [current] if isinstance(current, Profile) else []
    try:
        profiles, _ = load_profiles(repl.agent.settings.home, repl.agent.settings.workdir)
    except Exception:
        return found
    return found + list(profiles.values())


def _price_profile(profiles: list[Profile], model: str) -> Profile | None:
    """按模型名找配了价格的配置档（会话文件只记模型名）。"""
    priced = [p for p in profiles if p.model == model and p.price_in is not None and p.price_out is not None]
    return priced[0] if priced else None


# ---------- /stats ----------


@dataclass
class Bucket:
    sessions: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    costs: dict[str, float] = field(default_factory=dict)
    unpriced: int = 0  # 没法算钱的会话数（没配价格、或旧会话没记模型）

    def add(self, usage: dict[str, int], cost: tuple[float, str] | None) -> None:
        self.tokens_in += _tokens_in(usage)
        self.tokens_out += int(usage.get("output_tokens", 0))
        if cost is None:
            self.unpriced += 1
        else:
            self.costs[cost[1]] = self.costs.get(cost[1], 0) + cost[0]


@dataclass
class SessionRecord:
    when: _dt.datetime
    project: str
    by_model: dict[str, dict[str, int]]


def collect_sessions(home: Path) -> list[SessionRecord]:
    """读出全部项目的全部会话（~/.heng-agent/sessions/*/*.json）的用量。时间按会话文件最后更新的时候算。"""
    records = []
    for path in sorted((home / "sessions").glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        usage = data.get("usage") or {}
        by_model = data.get("usage_by_model") or ({UNKNOWN_MODEL: usage} if _tokens_in(usage) else {})
        if not by_model:
            continue
        when = _dt.datetime.fromtimestamp(path.stat().st_mtime)
        project = Path(str(data.get("workdir") or path.parent.name)).name
        records.append(SessionRecord(when, project, by_model))
    return records


def show_stats(repl: Repl) -> None:
    """跨会话的历史统计：今天 / 近 7 天 / 近 30 天 / 全部的会话数、token、花费；近 30 天按项目、按模型排前几。"""
    c, a = repl.console, repl.agent
    records = collect_sessions(a.settings.home)
    if not records:
        c.print("还没有会话记录。")
        return
    profiles = _profiles(repl)
    now = _dt.datetime.now()
    periods = {"今天": now.replace(hour=0, minute=0, second=0, microsecond=0), "近 7 天": now - _dt.timedelta(days=7),
               "近 30 天": now - _dt.timedelta(days=30), "全部": _dt.datetime.min}  # fmt: skip
    buckets = {name: Bucket() for name in periods}
    projects: dict[str, int] = {}
    models: dict[str, int] = {}
    for record in records:
        for name, since in periods.items():
            if record.when >= since:
                buckets[name].sessions += 1
                for model, usage in record.by_model.items():
                    profile = _price_profile(profiles, model)
                    cost = profile.cost(usage) if profile is not None else None
                    buckets[name].add(usage, (cost, profile.currency) if cost is not None and profile else None)
        if record.when >= periods["近 30 天"]:
            for model, usage in record.by_model.items():
                tokens = _tokens_in(usage) + int(usage.get("output_tokens", 0))
                projects[record.project] = projects.get(record.project, 0) + tokens
                models[model] = models.get(model, 0) + tokens
    table = Table(title="用量统计", title_justify="left", show_edge=False, header_style="bold")
    for column in ("", "会话", "输入 token", "输出 token", "花费"):
        table.add_column(column, justify="right" if column else "left")
    for name, b in buckets.items():
        money = _money(b.costs) or ("—" if b.sessions else "")
        if b.costs and b.unpriced:
            money += "（部分未计价）"
        table.add_row(name, str(b.sessions), human_tokens(b.tokens_in), human_tokens(b.tokens_out), money)
    c.print(table)
    for title, ranking in (("近 30 天按项目", projects), ("近 30 天按模型", models)):
        if ranking:
            top = sorted(ranking.items(), key=lambda kv: -kv[1])[:5]
            c.print(f"[bold]{title}[/]  " + "  ".join(f"{escape(k)} [dim]{human_tokens(v)}[/]" for k, v in top))
    c.print("[dim]时间按会话最后更新的那天算；花费按配置档里填的价格估算（没填价格、或旧会话没记模型的不计价）[/]")


# ---------- /hooks ----------

EVENT_LABEL = {"pre_tool": "工具执行前", "post_tool": "工具执行后", "user_prompt_submit": "发送消息时",
               "stop": "回复结束时", "session_start": "会话开始时"}  # fmt: skip


def show_hooks(repl: Repl) -> None:
    """生效的钩子：按事件分组，写出匹配哪些工具、命令、来自哪里（heng / Claude Code、用户 / 项目）；
    没信任的项目钩子、按 claude_hooks_skip 跳过的也列出来，免得以为它们在跑。"""
    c, a = repl.console, repl.agent
    active = a.hooks.hooks
    if not active and not repl.untrusted_hooks:
        c.print("没有钩子。在 config.json 的 hooks 里配置（见 README），或在 Claude Code 的 settings.json 里配置。")
    for event, label in EVENT_LABEL.items():
        hooks = [h for h in active if h.event == event]
        if not hooks:
            continue
        c.print(f"[bold]{label}[/] [dim]{event}[/]")
        for h in hooks:
            origin = ("Claude Code · " if h.style == "claude" else "") + ("项目" if h.source == "project" else "用户")
            match = "" if h.match == ".*" else f"[cyan]{escape(h.match)}[/]  "
            command = h.command if len(h.command) <= 90 else h.command[:87] + "…"
            c.print(f"  {match}{escape(command)}  [dim]{origin}[/]")
    if repl.untrusted_hooks:
        untrusted = len(repl.untrusted_hooks)
        c.print(f"[yellow]项目里还有 {untrusted} 个钩子没确认信任，没有生效[/]（重新打开 heng-agent 时会问）")
    skip = a.settings.claude_hooks_skip
    if skip:
        c.print(f"[dim]按 settings.claude_hooks_skip 跳过命令里含这些文字的 Claude 钩子：{escape('、'.join(skip))}[/]")

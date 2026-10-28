"""会话与模型：/resume 选会话、/rewind 回退、/context 上下文占用、/model 选模型。

从 repl.py 拆出，作为 Repl 的 mixin：方法里的 self 就是 Repl，方法名与行为都没变。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from rich.markup import escape

from heng_agent.core.errors import AgentError
from heng_agent.model.client import list_models
from heng_agent.session import SessionStore
from heng_agent.ui.config_cmd import ConfigCommand
from heng_agent.ui.prompt import human_tokens
from heng_agent.ui.render import (
    _PLAIN,
)
from heng_agent.ui.select import Option
from heng_agent.ui.session_tools import (
    context_rows,
)


class SessionMixin:
    """会话与模型：/resume 选会话、/rewind 回退、/context 上下文占用、/model 选模型。"""

    def pick_model(self, name: str = "") -> None:
        """/model：不带名字就向服务商要模型列表、方向键选；带名字直接切。当前会话立即生效，再问要不要存成默认。"""
        a, c = self.agent, self.console
        profile = getattr(a.client, "profile", None)
        if profile is None:
            c.print("[yellow]当前模型客户端不支持切换模型名[/]")
            return
        if not name:
            c.print("[dim]正在向服务商取可用模型……[/]")
            try:
                models = list_models(profile)
            except AgentError as exc:
                c.print(f"[yellow]{escape(str(exc))}[/]，可以直接写：/model 模型名")
                return
            if not models:
                c.print("[yellow]服务商没返回模型列表[/]，可以直接写：/model 模型名")
                return
            current = models.index(profile.model) if profile.model in models else 0
            options = [Option(m, m, detail="当前" if m == profile.model else "") for m in models]
            picked = self._choose(f"选择模型（{self.provider_name or '当前服务商'}）", options, default=current)
            if picked is _PLAIN:
                for i, m in enumerate(models, 1):
                    c.print(f"  {i:>2} {escape(m)}")
                raw = self._ask("选哪个（编号或名字，回车取消）> ").strip()
                picked = models[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(models) else (raw or None)
            if not picked:
                c.print("[dim]已取消[/]")
                return
            name = str(picked)
        a.client.profile = profile.model_copy(update={"model": name})  # type: ignore[attr-defined]
        c.print(f"模型已切换为 [bold]{escape(name)}[/]（当前会话）")
        if not self.provider_name or name == profile.model:
            return
        keep = self._choose(f"也存成 {self.provider_name} 的默认模型吗？", [
            Option("no", "只在这次会话里用", "n"), Option("yes", "存成默认（以后启动也用它）", "y"),
        ])  # fmt: skip
        if keep is _PLAIN:
            keep = "yes" if self._ask("也存成默认模型吗？[y/N] > ").strip().lower() in ("y", "yes", "是") else "no"
        if keep == "yes":
            try:
                ConfigCommand(self).save_model(self.provider_name, name)
                c.print(f"[green]✓ 已存成 {escape(self.provider_name)} 的默认模型[/]")
            except AgentError as exc:
                c.print(f"[red]{escape(str(exc))}[/]")

    def pick_session(self, keyword: str = "") -> None:
        """/resume [关键词]：列出这个目录的会话（按名字 / 第一句话筛选），选一个接着聊。"""
        a, c = self.agent, self.console
        current = a.session.id if a.session else ""
        sessions = [s for s in SessionStore.all(a.settings.home, a.settings.workdir)
                    if not keyword or keyword.lower() in f"{s.title} {s.first_question}".lower()]  # fmt: skip
        if not sessions:
            c.print(f"没有{'对得上「' + escape(keyword) + '」的' if keyword else ''}会话。")
            return

        def label(s: Any) -> str:
            name = s.title or (s.first_question.strip().splitlines() or ["（没有提问）"])[0]
            return name[:50]

        def detail(s: Any) -> str:
            when = _dt.datetime.fromtimestamp(s.updated).strftime("%m-%d %H:%M")
            return f"{when} · {s.messages} 条{' · 当前' if s.id == current else ''}"

        options = [Option(s.id, label(s), detail=detail(s)) for s in sessions]
        picked = self._choose("接着哪个会话聊？", options)
        if picked is _PLAIN:
            for i, s in enumerate(sessions, 1):
                c.print(f"  {i:>2} {escape(label(s))}  [dim]{detail(s)}[/]")
            raw = self._ask("选哪个（编号，回车取消）> ").strip()
            picked = sessions[int(raw) - 1].id if raw.isdigit() and 1 <= int(raw) <= len(sessions) else None
        if not picked:
            c.print("[dim]已取消[/]")
            return
        if picked == current:
            c.print("就是当前这个会话。")
            return
        count = a.resume(SessionStore(a.settings.home, a.settings.workdir, str(picked)))
        c.print(f"已接上会话 {escape(str(picked))}（{count} 条消息）。它之前的提问不能 /undo 或 /rewind。")

    def show_context(self) -> None:
        """/context：上下文被什么占着。各部分按字符数从模型报的真实总数里分。"""
        a, c = self.agent, self.console
        specs = a.tool_specs()  # 实际发给模型的声明：MCP 按需加载时没加载的不算
        rows = context_rows(a.system_prompt(), specs, a.messages, a.last_input_tokens)
        total, window = sum(n for _, n in rows), a.context_window
        basis = "按最近一次请求的真实用量" if a.last_input_tokens else "还没发过请求，按字数粗估"
        c.print(
            f"[bold]上下文[/] {human_tokens(total)} / {human_tokens(window)}（{total / window * 100:.0f}%，{basis}）"
        )
        for name, n in rows:
            width = round(n / window * 40) if window else 0
            bar = "█" * width + "[dim]" + "·" * (40 - width) + "[/]"
            c.print(f"  {name:<10}{bar} {human_tokens(n):>6}")
        c.print(f"[dim]到 {a.settings.compact_ratio:.0%} 会自动压缩；/compact [要求] 手动压缩[/]")

    def rewind(self) -> None:
        """/rewind 或空框双击 ESC：选一问，对话与代码一起回到它之前；那一问的原话放回输入框便于改了重发。"""
        a, c = self.agent, self.console
        titles = a.turn_titles()
        if not titles:
            c.print("这次启动后还没有可以回退的提问（接上的旧会话里的提问不在其内）。")
            return
        options = [Option(str(i), f"第 {i + 1} 问：{t[:60]}", detail="最近" if i == len(titles) - 1 else "")
                   for i, t in enumerate(titles)]  # fmt: skip
        picked = self._choose("回退到哪一问之前？（对话与它改过的文件一起退回）", options, default=len(titles) - 1)
        if picked is _PLAIN:
            raw = self._ask(f"回退到第几问之前（1-{len(titles)}，回车取消）> ").strip()
            picked = str(int(raw) - 1) if raw.isdigit() and 1 <= int(raw) <= len(titles) else None
        if picked is None:
            c.print("[dim]已取消[/]")
            return
        index = int(str(picked))
        question = titles[index]
        full = a.messages[a.turns[index]["start"]]["content"][0].get("text", question)
        restored = a.rewind(index)
        files = "、".join(restored) if restored else "无"
        c.print(f"已回退到第 {index + 1} 问之前；恢复的文件：{escape(files)}。那一问放回了输入框，可以改了再发。")
        c.print("[dim]bash 命令造成的改动（删文件、git 操作、构建产物）无法撤销。[/]")
        self._prefill = full

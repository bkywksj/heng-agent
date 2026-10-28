"""记忆：# 开头快速记一条、/memory 查看与编辑说明文件。

从 repl.py 拆出，作为 Repl 的 mixin：方法里的 self 就是 Repl，方法名与行为都没变。
"""

from __future__ import annotations

from rich.markup import escape

from heng_agent.context import (
    MEMORY_WARN_TOKENS,
    append_memory,
    estimate_tokens,
    load_memories,
    memory_files,
)
from heng_agent.core.errors import AgentError
from heng_agent.ui.prompt import human_tokens
from heng_agent.ui.render import (
    _PLAIN,
    edit_in_editor,
    file_link,
)
from heng_agent.ui.select import Option


class MemoryMixin:
    """记忆：# 开头快速记一条、/memory 查看与编辑说明文件。"""

    def quick_memory(self, text: str) -> None:
        """`# 这个项目用 pnpm`：选记到项目还是用户的 HENG.md，写完重建系统提示词，下一问就生效。"""
        files = memory_files(self.agent.settings)
        picked = self._choose("记到哪里？", [
            Option("project", "项目记忆", "p", f"{files['project']}（跟着仓库走）"),
            Option("user", "用户记忆", "u", f"{files['user']}（所有项目共用）"),
        ])  # fmt: skip
        if picked is _PLAIN:
            raw = self._ask("记到哪里？p 项目 / u 用户（回车取消）> ").strip().lower()
            picked = {"p": "project", "u": "user"}.get(raw)
        if picked not in files:
            self.console.print("[dim]已取消[/]")
            return
        try:
            append_memory(files[str(picked)], text)
        except (AgentError, OSError) as exc:
            self.console.print(f"[red]{escape(str(exc))}[/]")
            return
        self.agent.reload_system()
        self.console.print(f"[green]✓ 已记入 {file_link(str(files[str(picked)]), self.agent.settings.workdir)}[/]")

    def memory_command(self, rest: str) -> None:
        """/memory：看两份记忆；/memory edit project|user：用编辑器改，改完重建系统提示词。"""
        c, files = self.console, memory_files(self.agent.settings)
        sub, _, which = rest.partition(" ")
        which = which.strip() or "project"
        if sub == "edit":
            if which not in files:
                c.print("用法：/memory edit project 或 /memory edit user")
                return
            path = files[which]
            old = path.read_text(encoding="utf-8-sig") if path.is_file() else "# 记忆\n"
            new = edit_in_editor(old)
            if new is None:
                c.print("[dim]没有改动[/]")
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new.rstrip("\n") + "\n", encoding="utf-8")
            self.agent.reload_system()
            c.print(f"[green]✓ 已更新 {file_link(str(path), self.agent.settings.workdir)}，下一问生效[/]")
            return
        self.memory_overview()

    def memory_overview(self) -> None:
        """/memory：读进来的全部说明文件——按优先级从低到高（冲突时以后面的为准）、各自多大、# 快速记忆写到哪里。
        几份说明并存时用户看得清每份占多少、谁说了算；合计太大时提醒（每次对话都带着）。"""
        c, a = self.console, self.agent
        loaded = load_memories(a.settings)
        files = memory_files(a.settings)
        if not loaded:
            c.print("[dim]还没有说明文件。输入 /init 让 AI 熟悉项目、生成 AGENTS.md；或用 # 开头的一句话快速记一条[/]")
        else:
            c.print("[bold]常驻说明[/]  [dim]按优先级从低到高，内容冲突时以后面的为准[/]")
            total = 0
            for i, (path, block) in enumerate(loaded, 1):
                tokens = estimate_tokens(block)
                total += tokens
                c.print(f"  {i}. {file_link(str(path), a.settings.workdir)}  [dim]约 {human_tokens(tokens)} token[/]")
            share = f"，占上下文 {total / a.context_window:.0%}" if a.context_window else ""
            c.print(f"  [dim]合计约 {human_tokens(total)} token{share}，每次对话都带着[/]")
            if total > MEMORY_WARN_TOKENS:
                c.print(f"[yellow]说明文件偏大（超过 {human_tokens(MEMORY_WARN_TOKENS)} token）：可以精简，"
                        "或把重复的合到一份（如 CLAUDE.md 里只写一行 @AGENTS.md）；"
                        "不想带 ~/.claude/CLAUDE.md 就 /config set settings.claude_user_config false[/]")  # fmt: skip
        project, user = files["project"], files["user"]
        note = "" if project.is_file() else "（还没有，第一次记的时候新建）"
        c.print(f"[bold]写到哪里[/]  # 快速记忆与 /memory edit project → {file_link(str(project), a.settings.workdir)}"
                f"[dim]{note}[/]；/memory edit user → {escape(str(user))}")  # fmt: skip
        c.print("[dim]子目录里的 HENG.md / AGENTS.md / CLAUDE.md 在读到那个目录的文件时自动附上，比根目录的更具体[/]")

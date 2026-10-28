"""其余子命令：/worktree、/agents、/think、/tasks、/terminal-setup、/doctor copy。

从 repl.py 拆出，作为 Repl 的 mixin：方法里的 self 就是 Repl，方法名与行为都没变。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from rich.cells import cell_len
from rich.markup import escape
from rich.panel import Panel

from heng_agent import worktree
from heng_agent.agents import create_agent, discover_agents
from heng_agent.core import logs
from heng_agent.core.errors import AgentError
from heng_agent.model.client import make_client
from heng_agent.model.thinking import EFFORT_NAME, EFFORTS
from heng_agent.model.thinking import describe as describe_thinking
from heng_agent.tools.background import describe
from heng_agent.tools.subagent import SubAgentTool
from heng_agent.ui import agents_view, btw, terminal_setup
from heng_agent.ui.doctor import report as doctor_report
from heng_agent.ui.render import (
    _PLAIN,
    MIN_THINKING_BUDGET,
    ReplyMarkdown,
    edit_in_editor,
    file_link,
)
from heng_agent.ui.select import Option
from heng_agent.ui.session_tools import (
    code_blocks,
    copy_text,
    replies,
)


class CommandsMixin:
    """其余子命令：/worktree、/agents、/think、/tasks、/terminal-setup、/doctor copy。"""

    def terminal_setup(self) -> None:
        """/terminal-setup：给本机的 Windows Terminal 加上 Shift+Enter 换行（见 terminal_setup 模块的说明）。"""
        c = self.console
        found = terminal_setup.find_settings()
        if not found:
            c.print("没找到 Windows Terminal 的设置文件。其他终端里用 Ctrl+J 或 Alt+Enter 换行。")
            return
        for label, path in found:
            try:
                note = terminal_setup.install(path)
                c.print(f"[green]✓ {escape(label)}：{escape(note)}[/]")
            except AgentError as exc:
                c.print(f"[yellow]{escape(label)}：{escape(str(exc))}[/]")
        c.print("[dim]Windows Terminal 会自动重新加载设置，现在就能用 Shift+Enter 换行。"
                "在别的程序里 Shift+Enter 会发送换行符（多数程序里等同回车）。[/]")  # fmt: skip

    def worktree_command(self, rest: str) -> None:
        """/worktree 列出；/worktree new 名字 新建并切过去；/worktree switch 名字|main；/worktree remove 名字。"""
        a, c = self.agent, self.console
        try:
            root = worktree.repo_root(a.settings.workdir)
        except AgentError as exc:
            c.print(f"[yellow]这里不是 git 仓库，用不了工作树：{escape(str(exc))}[/]")
            return
        sub, _, name = rest.partition(" ")
        name = name.strip()
        try:
            if sub == "new" and name:
                created = worktree.create(root, name)
                self.switch_workdir(created.path, f"工作树 {name}（分支 {created.branch}）")
                c.print(f"[dim]改完用 /commit 提交，再 /worktree switch main 回主目录 git merge {created.branch}[/]")
            elif sub == "switch" and name:
                target = root if name == "main" else worktree.path_of(root, name)
                if not any(w.path == target.resolve() for w in worktree.list_worktrees(root)):
                    c.print(f"没有叫 {escape(name)} 的工作树。/worktree new {escape(name)} 新建一个")
                    return
                self.switch_workdir(target, "主目录" if name == "main" else f"工作树 {name}")
            elif sub == "remove" and name:
                if worktree.path_of(root, name) == a.settings.workdir.resolve():
                    c.print("[yellow]正在这个工作树里，先 /worktree switch main 再删[/]")
                    return
                c.print(escape(worktree.remove(root, name)))
            elif not rest:
                current = a.settings.workdir.resolve()
                for w in worktree.list_worktrees(root):
                    mark = "[green]●[/]" if w.path == current else " "
                    label = "主目录" if w.main else w.path.name
                    c.print(f" {mark} [bold]{escape(label)}[/]  [cyan]{escape(w.branch or '（分离 HEAD）')}[/]  "
                            f"[dim]{escape(str(w.path))}[/]")  # fmt: skip
                c.print("[dim]/worktree new 名字 新建并切过去；/worktree switch 名字|main；/worktree remove 名字[/]")
            else:
                c.print("用法：/worktree、/worktree new 名字、/worktree switch 名字|main、/worktree remove 名字")
        except AgentError as exc:
            c.print(f"[red]{escape(str(exc))}[/]")

    def agents_command(self, rest: str) -> None:
        """/agents 角色总览（方向键选一个看详情）；/agents 名字 直接看详情；/agents new 名字 建模板并用编辑器打开。"""
        a, c = self.agent, self.console
        sub, _, name = rest.partition(" ")
        if sub == "new":
            name = name.strip()
            if not re.fullmatch(r"[\w\-]+", name):
                c.print("用法：/agents new 名字（字母、数字、中文、_ -）")
                return
            path = create_agent(a.settings.workdir, name)
            original = path.read_text(encoding="utf-8")
            edited = edit_in_editor(original)
            if edited is not None:
                path.write_text(edited.rstrip("\n") + "\n", encoding="utf-8")
            self.reload_agents()
            note = "[dim]模型现在就能派它了[/]"
            c.print(f"[green]✓ 角色 {escape(name)}：{file_link(str(path), a.settings.workdir)}[/]  {note}")
            return
        self.reload_agents()  # 手动改过文件也能马上生效
        if not a.agent_defs:
            c.print("没有自定义角色。/agents new 名字 建一个（如 测试员、审查员），"
                    "或放在 .heng-agent/agents/名字.md（项目）、~/.heng-agent/agents/（用户）。")  # fmt: skip
            return
        roles = list(a.agent_defs.values())
        if sub:  # /agents 名字
            role = a.agent_defs.get(rest.strip())
            if role is None:
                c.print(f"没有叫 {escape(rest.strip())} 的角色。/agents 看全部")
            else:
                agents_view.show_role(self, role)
            return
        if self._plain_input:  # 选择器用不了：纯文字一行一个
            agents_view.show_summary(self, roles)
            return
        # 一行一个角色（名字 · 只读/会改动 · 来源 · 第一句说明），选中再看完整说明——以前全部平铺，长描述折成好几行，很乱
        c.print(agents_view.headline(roles))
        width = max(cell_len(r.name) for r in roles)
        budget = agents_view.options_budget(self, width)
        options = [Option(r.name, agents_view.option_label(r, width), detail=agents_view.option_detail(r, self, budget))
                   for r in roles]  # fmt: skip
        picked = self._choose("选一个角色看完整说明（ESC 退出）", options)
        if picked is _PLAIN:
            agents_view.show_summary(self, roles, with_headline=False)
            return
        if picked:
            agents_view.show_role(self, a.agent_defs[str(picked)])
        c.print(f"[dim]{agents_view.HINT}[/]")

    def reload_agents(self) -> None:
        """重新读角色文件，并把 task 工具换成带新角色列表的（工具说明里列着角色，模型靠它挑）。"""
        a = self.agent
        a.agent_defs = discover_agents(a.settings.home, a.settings.workdir, claude_user=a.settings.claude_user_config)
        if "task" in a.tools:
            a.tools["task"] = SubAgentTool(a._spawn_subagent, a._add_usage, a.agent_defs)

    def think_command(self, rest: str) -> None:
        """/think 切换思考模式；/think on|off；/think 16000 设预算并打开；/think show 看最近一次思考。"""
        a, c = self.agent, self.console
        arg = rest.strip().lower()
        if arg == "show":
            if self.last_thought:
                c.print(Panel(escape(self.last_thought), title="最近一次思考", border_style="grey50"))
            else:
                c.print("这次会话还没有思考过。")
            return
        if arg.isdigit():
            if int(arg) < MIN_THINKING_BUDGET:
                c.print(f"[yellow]思考预算至少 {MIN_THINKING_BUDGET}（Anthropic 的要求）[/]")
                return
            a.thinking_budget, a.thinking = int(arg), True
        elif arg in ("on", "off"):
            a.thinking = arg == "on"
        elif not arg:
            a.thinking = not a.thinking
        else:
            c.print("用法：/think（切换）、/think on|off、/think 16000（设预算）、/think show")
            return
        c.print(f"✻ 思考：{escape(describe_thinking(a.model_name, a.thinking, a.effort))}")
        profile = getattr(a.client, "profile", None)
        if a.thinking and profile is not None and profile.protocol == "openai":
            c.print("[dim]这个服务商走 OpenAI 协议：思不思考由模型本身决定（如 deepseek-reasoner 总会思考），"
                    "/think 对它不起作用；模型输出的思考过程照样会显示[/]")  # fmt: skip

    def effort_command(self, rest: str) -> None:
        """/effort：选思考强度；/effort high 直接设；/effort auto 回到服务默认。设了强度顺手打开思考。"""
        a, c = self.agent, self.console
        arg = rest.strip().lower()
        if not arg:
            options = [Option(e, f"{e}  {EFFORT_NAME[e]}") for e in EFFORTS] + [Option("auto", "auto  服务默认")]
            # default 是序号：以前传了强度的名字，选择器里报错、被当成「这个终端用不了选择器」，
            # 输入框也跟着退回成普通的逐行输入（真实终端里实测）
            current = EFFORTS.index(a.effort) if a.effort in EFFORTS else len(EFFORTS)
            picked = self._choose("思考强度（越高想得越多、越慢、越贵）", options, default=current)
            if picked is None or picked is _PLAIN:
                c.print(
                    f"思考强度：{escape(a.effort or 'auto（服务默认）')}。用法：/effort low|medium|high|xhigh|max|auto"
                )
                return
            arg = str(picked)
        if arg not in (*EFFORTS, "auto"):
            c.print("用法：/effort low|medium|high|xhigh|max|auto")
            return
        a.effort = None if arg == "auto" else arg  # type: ignore[assignment]
        if a.effort:
            a.thinking = True  # 设强度就是想让它想：顺手打开，不然还得再 /think on
        c.print(f"✻ 思考：{escape(describe_thinking(a.model_name, a.thinking, a.effort))}")
        profile = getattr(a.client, "profile", None)
        if a.effort and profile is not None and profile.protocol == "openai" and a.effort in ("xhigh", "max"):
            c.print("[dim]OpenAI 协议只有 low / medium / high：按 high 发[/]")

    def copy_command(self, rest: str) -> None:
        """/copy [N]：复制倒数第 N 条回复（默认 1）的原文（Markdown，不带屏幕上的换行与空格）。

        回复里有代码块时先让你选：整条回复，还是其中一段代码（照 Claude Code）。选择器用不了的终端直接复制整条。"""
        c = self.console
        arg = rest.strip()
        if arg and not arg.isdigit():
            c.print("用法：/copy [N]——N 是倒数第几条回复，默认 1（上一条）")
            return
        n = int(arg or 1)
        found = replies(self.agent.messages)
        if not found:
            c.print("还没有可以复制的回复。")
            return
        if not 1 <= n <= len(found):
            c.print(f"[yellow]只有 {len(found)} 条回复，没有倒数第 {n} 条[/]")
            return
        reply = found[n - 1]
        text, what = reply, "整条回复"
        blocks = code_blocks(reply)
        if blocks:
            options = [Option("all", "整条回复", detail=f"{len(reply.splitlines())} 行")]
            for i, (lang, code) in enumerate(blocks):
                first = next((line.strip() for line in code.splitlines() if line.strip()), "")
                preview = first if cell_len(first) <= 50 else first[:48] + "…"
                options.append(Option(str(i), f"代码 {i + 1}{f'（{lang}）' if lang else ''}：{preview}",
                                      detail=f"{len(code.splitlines())} 行"))  # fmt: skip
            picked = self._choose("复制哪一段（ESC 取消）", options)
            if picked is None:
                return
            if isinstance(picked, str) and picked != "all":
                lang, text = blocks[int(picked)]
                what = f"代码 {int(picked) + 1}"
        ok, note = copy_text(text)
        c.print(f"[{'green' if ok else 'red'}]{escape(note)}[/]" + (f"[dim]（{what}）[/]" if ok else ""))

    def btw_command(self, rest: str) -> None:
        """/btw 问题：旁支问题。在后台线程里问（不打断正在进行的回答），答完打印在一个框里，不进对话历史。"""
        a, c = self.agent, self.console
        question = rest.strip()
        if not question:
            c.print("用法：/btw 问题——随口问一句，不打断正在进行的回答、不进对话历史、不调工具")
            return
        client = self._btw_client()
        if client is None:
            c.print("[yellow]当前的模型客户端用不了 /btw[/]")
            return
        # 在界面线程里先取好快照：后台线程问的时候，正在进行的这一问可能还在往对话里加东西
        messages, system, history = list(a.messages), a.system_prompt(), list(self._btw_log)
        c.print("[dim]旁支问题已发出：答完显示在这里，不打断正在进行的回答，也不进对话历史[/]")

        def work() -> None:
            try:
                answer = btw.ask(client, system, messages, history, question)
            except Exception as exc:  # 网络、服务报错：说一声，别让后台线程带着异常悄悄退出
                logs.log.warning("/btw 出错：%s", exc)
                self._btw_show(f"[red]旁支问题没问成：{escape(str(exc))}[/]")
                return
            title = question if len(question) <= 40 else question[:39] + "…"
            self._btw_show(Panel(ReplyMarkdown(answer or "（没有回答）"), title=f"[bold]旁支问题：{escape(title)}[/]",
                                 subtitle="[dim]不进对话历史[/]", border_style="magenta", padding=(0, 1)))  # fmt: skip
            self._btw_log.append((question, answer))  # 显示之后再记：记上了就表示已经显示出来

        threading.Thread(target=work, name="heng-btw", daemon=True).start()

    def _btw_client(self) -> Any:
        """/btw 单独用一个模型客户端：正在进行的那一问还在用原来的，共用的话 ESC 打断主回答会把它也取消。测试里换掉。"""
        profile = getattr(self.agent.client, "profile", None)
        return make_client(profile, self.agent.settings.max_tokens) if profile is not None else None

    def _btw_show(self, renderable: Any) -> None:
        """把旁支问题的回答显示出来（后台线程里调）：回答中经输入框打印到上方；平时输入框开着就收起来打印。"""
        box = self._box

        def show() -> None:
            self.console.print()  # 和上面的内容隔开一行
            self.console.print(renderable)
            self._block_start = False

        if box is not None and box.turn is None and box.print_above(show):
            return
        show()  # 回答中 console 是写到输入框上方的那个；没有输入框就直接打印

    def tasks_command(self, rest: str) -> None:
        """/tasks：列出后台任务；/tasks kill 任务号：停掉。"""
        c, manager = self.console, self.agent.ctx.tasks
        sub, _, task_id = rest.partition(" ")
        if sub == "kill" and task_id.strip():
            task = manager.kill(task_id)
            c.print(f"已停止 {escape(task.id)}：{escape(task.command)}" if task else f"没有后台任务 {escape(task_id)}")
            return
        if not manager.tasks:
            c.print("没有后台任务。让模型用 bash 的 run_in_background 启动，或在命令跑着时按 Ctrl+B 转到后台。")
            return
        for task in manager.tasks.values():
            color = "green" if task.code is None else "dim"
            c.print(f"[{color}]{escape(describe(task, limit=100))}[/]")
        c.print("[dim]/tasks kill 任务号 停止；退出 heng-agent 时会停掉全部后台任务[/]")

    def copy_diagnosis(self, checks: list[tuple[str, str, str]]) -> None:
        """/doctor copy：诊断报告放进剪贴板；剪贴板用不了就存成文件，告诉用户在哪。"""
        home = self.agent.settings.home
        text = doctor_report(checks, home)  # type: ignore[arg-type]
        ok, note = copy_text(text)
        if ok:
            self.console.print(f"[green]✓ 已复制诊断信息[/]（{len(text.splitlines())} 行，含最近的日志；"
                               "用户目录已换成 ~），可以直接粘贴到反馈里")  # fmt: skip
            return
        path = home / "logs" / f"doctor-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.console.print(f"[yellow]没能复制（{escape(note)}）[/]，诊断信息存到了 {file_link(str(path), home)}")

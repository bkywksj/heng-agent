"""子智能体面板、后台子智能体做完时的通知、查看子智能体做了什么（Repl 继承的 mixin）。

照 Claude Code：
- 状态栏下面列出 ● main 和正在跑的子智能体（任务、用时、token），在输入框最后一行按 ↓ 选中，Enter 查看；
- 后台子智能体（task 的 run_in_background）做完时：主智能体正在回答，就在这批工具跑完时把结论插进这一问；
  空闲，就叫醒输入框、自动发起一问把结论交给它（框里敲了一半的字留着）。

注意：本模块只能从 render 导入，不能导入 repl（repl 继承它，会循环引用）。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from heng_agent.ui.render import ReplyMarkdown, elapsed_text

if TYPE_CHECKING:
    from rich.console import Console

    from heng_agent.tools.subagent import SubRun

#: 交给主智能体的「后台子智能体做完了」都以它开头：界面据此显示成一行提示，而不是你说的话
NOTICE_PREFIX = "【后台子智能体完成】"
#: 查看子智能体时，一段文字最多显示多少行
VIEW_TEXT_LINES = 30


def notice_text(run: SubRun) -> str:
    """交给主智能体的话（照 Claude Code 的 task-notification）。"""
    state = "没做完" if run.error else f"做完了（用时 {elapsed_text(run.elapsed)} · {run.rounds} 轮）"
    return f"{NOTICE_PREFIX}#{run.id}「{run.description}」{state}。结论：\n\n{run.result}"


def notice_line(text: str) -> str:
    """界面上显示的那一行（rich 标记）：只要第一行，结论本身不刷屏（Enter 查看、/copy 都看得到）。"""
    head = text.removeprefix(NOTICE_PREFIX).split("。结论：", 1)[0]
    return f"[magenta]◆ 后台子智能体 {escape(head)}[/][dim] · 结论已交给主智能体[/]"


class SubagentsMixin:
    """由 Repl 继承。用到的属性（agent、console、_box……）都在 Repl.__init__ 里建好。"""

    console: Console
    agent: Any
    _box: Any

    def _setup_subagents(self) -> None:
        self._notices: list[str] = []
        self._notices_lock = threading.Lock()
        self.agent.background_notify = self._on_background_done

    # ---------- 后台做完 ----------

    def _on_background_done(self, run: SubRun) -> None:
        """后台线程里调：记下来；空闲时叫醒输入框（主循环接着发起一问），回答中等这批工具跑完时插进去。"""
        with self._notices_lock:
            self._notices.append(notice_text(run))
        box = self._box
        if box is not None and box.turn is None:
            box.signal("notify")

    def _take_notices(self) -> list[str]:
        with self._notices_lock:
            taken, self._notices[:] = list(self._notices), []
        return taken

    # ---------- 面板 ----------

    def _agent_rows(self) -> list[tuple[int, str]]:
        """正在跑的子智能体：(编号, 一行说明)。说明对齐，后面是用时与 token（照 Claude Code 的面板）。"""
        runs = self.agent.subruns.running()
        if not runs:
            return []
        names = [f"{r.role if r.role != '通用调研员' else 'general'}  {r.description}" for r in runs]
        width = min(max(cell_len(n) for n in names), 60)
        rows = []
        for run, name in zip(runs, names, strict=True):
            pad = " " * max(width - cell_len(name), 0)
            tokens = f" · ↓ {run.tokens / 1000:.1f}k tokens" if run.tokens else ""
            tag = " · 后台" if run.background else ""
            rows.append((run.id, f"{name}{pad}   {elapsed_text(run.elapsed)}{tokens}{tag}"))
        return rows

    # ---------- 查看 ----------

    def view_subagent(self, run_id: int) -> None:
        """面板里选中按 Enter：把这个子智能体到现在为止做了什么打印出来（读了哪些、搜了什么、说了什么）。"""
        run = self.agent.subruns.get(run_id)
        if run is None:
            return
        state = "正在跑" if run.running else ("没做完" if run.error else "做完了")
        body: list[Any] = []
        for kind, line in run.log[-40:]:
            if kind == "tool":
                body.append(Text(f"● {line}", style="grey50"))
                continue
            lines = line.strip().splitlines()
            if len(lines) > VIEW_TEXT_LINES:  # 一段很长的结论只看最后这些行（真实终端里一段就刷了一整屏）
                body.append(Text(f"…（前面 {len(lines) - VIEW_TEXT_LINES} 行省略）", style="dim"))
                lines = lines[-VIEW_TEXT_LINES:]
            body.append(ReplyMarkdown("\n".join(lines)))
        if not body:
            body.append(Text("（还没有动作）", style="dim"))
        from rich.console import Group

        title = f"[bold]子智能体 #{run.id}：{escape(run.description)}[/]"
        subtitle = f"[dim]{state} · {elapsed_text(run.elapsed)} · {run.tokens / 1000:.1f}k tokens[/]"
        panel = Panel(Group(*body), title=title, subtitle=subtitle, border_style="magenta", padding=(0, 1))
        real = getattr(self, "_turn_real_console", None) if self._box is not None and self._box.turn else None
        console = real or self.console

        def show() -> None:
            console.print()
            console.print(panel)

        if self._box is None or not self._box.print_above(show):
            show()

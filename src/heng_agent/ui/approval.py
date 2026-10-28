"""确认对话：改文件、跑命令前问用户，交计划时选按哪种模式执行。

从 repl.py 拆出，作为 Repl 的 mixin：方法里的 self 就是 Repl，方法名与行为都没变。
"""

from __future__ import annotations

import difflib
from typing import Any

from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax

from heng_agent.core.errors import AgentError
from heng_agent.permissions import Rules
from heng_agent.tools.base import Tool
from heng_agent.tools.plan import NAME as EXIT_PLAN
from heng_agent.ui.config_cmd import ConfigCommand
from heng_agent.ui.render import (
    _PLAIN,
    approval_body,
    tool_label,
)
from heng_agent.ui.select import Option


class ApprovalMixin:
    """确认对话：改文件、跑命令前问用户，交计划时选按哪种模式执行。"""

    def approve(self, tool: Tool, args: dict[str, Any]) -> Any:
        """确认框；允许后马上把等待动画重新打开。

        问之前会关掉动画（免得和选择器纠缠），以前允许后没再打开：工具可能要跑好几秒（MCP 调外部接口），
        这段时间屏幕没有任何动静，看着像卡住了。
        """
        answer = self._ask_approval(tool, args)
        if answer != "deny":
            self._spin_tool(tool.name, args)  # 计时从批准后开始：等你确认的时间不算工具的
        return answer

    def _ask_plan(self, args: dict[str, Any]) -> str:
        """模型交出计划：完整显示（Markdown），让用户选按哪种模式开始执行，或者继续规划。

        回答沿用确认对话的 once / always / deny（Agent 按它切模式，ACP 客户端也是这三种）。
        """
        c = self.console
        c.print(Panel(Markdown(str(args.get("plan", ""))), title="[bold]计划[/]", border_style="cyan", padding=(0, 1)))
        with self._pause_watcher():
            picked = self._choose("按这个计划开始吗？", [
                Option("once", "开始执行：改文件、跑命令前问我", "y", "切到「手动确认」"),
                Option("always", "开始执行：改文件不问，跑命令还问", "a", "切到「自动改文件」"),
                Option("deny", "继续规划", "n", "留在计划模式，接着说要改哪里（ESC 同样）"),
            ])  # fmt: skip
            if picked is _PLAIN:
                while True:  # 选择器用不了的终端：退回打字
                    raw = c.input("开始吗？[bold]y[/] 执行（改前问我）/ [bold]a[/] 执行（改文件不问）/ "
                                  "[bold]n[/] 继续规划 > ").strip().lower()  # fmt: skip
                    picked = {"y": "once", "yes": "once", "": "once", "a": "always", "n": "deny", "no": "deny"}.get(raw)
                    if picked:
                        break
        answer = str(picked or "deny")  # ESC = 继续规划
        if answer == "deny":
            c.print("[dim]→ 继续规划：说说要怎么改，它会接着完善计划[/]")
        return answer

    def _ask_approval(self, tool: Tool, args: dict[str, Any]) -> Any:
        self._unspin()  # 要问用户了：先停动画，否则会和输入提示纠缠在一起
        c = self.console
        if tool.name == EXIT_PLAN:
            return self._ask_plan(args)
        if tool.name == "edit_file":
            diff = difflib.unified_diff(
                str(args.get("old_string", "")).splitlines(), str(args.get("new_string", "")).splitlines(),
                fromfile=str(args.get("path")), tofile=str(args.get("path")), lineterm="",
            )  # fmt: skip
            body: Any = Syntax("\n".join(diff), "diff", theme="ansi_dark", word_wrap=True)
        elif tool.name == "write_file":
            content = str(args.get("content", ""))
            body = Syntax(content[:3000] + ("\n…" if len(content) > 3000 else ""), "text", word_wrap=True)
        else:
            body = escape(approval_body(tool, args))
        c.print(Panel(body, title=f"[bold]要执行 {escape(tool_label(tool.name))}[/]", border_style="yellow"))
        with self._pause_watcher():  # 问用户期间暂停 ESC 监听，免得它抢走按键
            picked = self._choose("允许吗？", [
                Option("once", "这次允许", "y"),
                Option("always", f"本会话一直允许 {tool.name}", "a"),
                Option("persist", f"以后都允许 {Rules.suggest(tool, args)[:60]}", "p", "写进用户配置"),
                Option("deny", "拒绝", "n", "ESC 也是拒绝"),
            ])  # fmt: skip
            if picked is not _PLAIN:
                answer = picked or "deny"  # ESC 取消 = 拒绝
                shown = dict(
                    once="这次允许", always="本会话一直允许", persist="以后都允许（已写进配置）", deny="已拒绝"
                )
                c.print(f"[dim]→ {shown[str(answer)]}[/]")
                return answer
            while True:  # 选择器用不了的终端：退回打字
                answer = (
                    c.input("允许吗？[bold]y[/] 这次 / [bold]a[/] 本会话一直允许 / [bold]n[/] 拒绝 > ").strip().lower()
                )
                if answer in ("y", "yes", "是", ""):
                    return "once"
                if answer in ("a", "always"):
                    return "always"
                if answer in ("n", "no", "否"):
                    return "deny"

    def _persist_rule(self, rule: str) -> None:
        """写配置失败（比如配置文件坏了）只提示：不能让错误冒到工具执行里把任务打断，这次的允许照样有效。"""
        try:
            ConfigCommand(self).add_rule("allow", rule)
        except AgentError as exc:
            self.console.print(f"[yellow]规则没能写进配置（这次仍然允许）：{escape(str(exc))}[/]")

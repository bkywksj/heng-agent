"""exit_plan_mode：计划模式里，模型读完代码后用它交出计划，由用户决定按哪种模式开始执行。

以前计划模式只是「只读」：模型出完方案就停了，用户得自己切模式、再说一遍「按刚才的做」。
现在模型交出计划 → 界面完整显示 → 用户选「开始执行（改文件前问我）」「开始执行（改文件不问）」或「继续规划」；
批准后权限模式当场切换，模型在同一轮里接着按计划做。

这个工具只在计划模式里提供给模型（Agent.tool_specs），确认与切换模式由 Agent._present_plan 处理，
不走一般的权限确认（那套只有「允许 / 拒绝」，表达不了「按哪种模式执行」）。
"""

from __future__ import annotations

from typing import Any

from heng_agent.core.types import ToolResult
from heng_agent.tools.base import Tool, ToolContext

NAME = "exit_plan_mode"
#: 用户的选择 → 批准后切到的权限模式。确认对话沿用 once / always / deny 三种回答（终端与 ACP 客户端都认）
APPROVED_MODE = {"once": "ask", "persist": "ask", "always": "accept-edits"}


class ExitPlanMode(Tool):
    name = NAME
    kind = "read"  # 本身不改任何东西；交给用户确认、切换模式由 Agent 处理
    description = (
        "计划模式里用：把代码读透之后，用它交出实施计划，请用户批准。计划用 Markdown 写清："
        "要改哪些文件、每处怎么改、按什么顺序、怎么验证（跑哪些测试）。用户批准后权限模式会切到可执行的模式，"
        "你接着按计划做；用户没批准就停下，等用户说要怎么改。只在计划模式里可用。"
    )
    input_schema = {
        "type": "object",
        "properties": {"plan": {"type": "string", "description": "实施计划（Markdown）"}},
        "required": ["plan"],
    }

    def summary(self, args: dict[str, Any]) -> str:
        first = next((ln.strip("# ").strip() for ln in str(args.get("plan", "")).splitlines() if ln.strip()), "")
        return first[:60]

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 正常由 Agent._present_plan 接管；走到这里说明是在别的地方（如子智能体）被直接调用了
        return ToolResult("交计划只在主对话的计划模式里有用：直接把计划写在回复里即可。", is_error=True)

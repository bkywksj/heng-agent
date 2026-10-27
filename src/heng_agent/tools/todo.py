"""任务清单工具：多步骤任务先列步骤，做一项勾一项，用户随时看得到进度，模型也不容易漏步骤。

每次传入**完整**清单（不是增删单项），不会和状态对不上。不改外部状态，不需要确认。
"""

from __future__ import annotations

from typing import Any

from heng_agent.core.types import ToolResult
from heng_agent.tools.base import Tool, ToolContext

MARK = {"pending": "☐", "in_progress": "▶", "done": "☑"}
#: 各家模型的习惯写法不同（Claude 爱用 completed）：同义词直接认，省掉一次报错重试
ALIASES = {
    "completed": "done", "complete": "done", "finished": "done",
    "in-progress": "in_progress", "doing": "in_progress", "active": "in_progress",
    "todo": "pending", "not_started": "pending",
}  # fmt: skip


def render(todos: list[dict[str, str]]) -> str:
    done = sum(t["status"] == "done" for t in todos)
    lines = [f"任务清单（{done}/{len(todos)} 完成）"]
    lines += [f"{MARK[t['status']]} {t['content']}" for t in todos]
    return "\n".join(lines)


class Todo(Tool):
    name = "todo"
    description = (
        "维护本次任务的步骤清单。三步以上的任务先用它列出步骤，开始做某一步时把它标成 in_progress，"
        "做完立刻标 done（同一时间只有一项 in_progress）。每次传完整清单。简单的一两步任务不用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "这一步要做什么"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def summary(self, args: dict[str, Any]) -> str:
        todos = args.get("todos") or []
        return f"{sum(t.get('status') == 'done' for t in todos)}/{len(todos)} 完成"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        todos = [
            {"content": str(t["content"]), "status": ALIASES.get(str(t["status"]).lower(), str(t["status"]).lower())}
            for t in args["todos"]
        ]
        bad = [t["status"] for t in todos if t["status"] not in MARK]
        if bad:
            return ToolResult(f"status 只能是 pending / in_progress / done，收到 {bad}", is_error=True)
        if sum(t["status"] == "in_progress" for t in todos) > 1:
            return ToolResult("同一时间只能有一项 in_progress：先把做完的标成 done", is_error=True)
        ctx.todos[:] = todos
        return ToolResult(render(todos))

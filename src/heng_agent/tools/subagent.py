"""子智能体工具：把一件调研 / 探索的事交给一个全新的、只读的智能体去做，只把结论带回来。

好处：主对话不被大量搜索、读文件的过程挤满；同一轮发出多个 task 时并行执行（agent.py 负责并发）。

子智能体只有只读工具（读文件、找文件、搜内容、读技能），也不能再派子智能体：
- 并行的子智能体若能改文件、跑命令，确认对话会互相打架，同时写同一个文件也会冲突；
- 只读就不需要用户确认，可以安全地并发。
需要改代码的事，等子智能体带回结论后由主智能体来做。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from heng_agent.core.types import TextDelta, ToolResult, TurnDone
from heng_agent.tools.base import Tool, ToolContext

SUBAGENT_PROMPT = """你是一个只读的调研子智能体，替主智能体完成下面这件事。你只能读文件、找文件、搜内容，
不能修改任何东西。做完后给出一份简洁、具体的结论：找到了什么（文件路径、函数名、行号）、得出什么判断。
结论会原样交给主智能体，它看不到你的中间过程，所以结论要能独立看懂。

任务：
"""
MAX_RESULT_CHARS = 20_000


class SubAgentTool(Tool):
    name = "task"
    kind = "read"  # 子智能体只读，不需要确认
    description = (
        "派一个只读的子智能体去完成一件调研 / 探索任务（在大项目里找相关代码、梳理调用关系、对比几处实现……），"
        "它独立搜索阅读，只把结论带回来，主对话不会被中间过程挤满。同一轮里发出多个 task 会并行执行。"
        "子智能体不能修改文件或跑命令；要改代码，等结论回来后自己改。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "三五个字概括这件事（显示给用户）"},
            "prompt": {"type": "string", "description": "交给子智能体的完整任务说明：要找什么、要回答什么问题"},
        },
        "required": ["description", "prompt"],
    }

    def __init__(
        self,
        spawn: Callable[[Any], tuple[Any, Any]],
        on_usage: Callable[[dict[str, int]], None] | None = None,
        agents: dict[str, Any] | None = None,
    ) -> None:
        """spawn(角色或 None) 返回 (子 Agent, 确认对话)；on_usage 把子智能体的用量记进主 Agent；
        agents：自定义角色（名字 → AgentDef），写进工具说明里让模型挑。

        用量用回调交回而不是存在工具对象上：多个子智能体并行时会互相覆盖。
        """
        self.spawn = spawn
        self.on_usage = on_usage
        self.agents = dict(agents or {})
        if self.agents:
            listing = "\n".join(
                f"- {a.name}：{a.description}（工具：{', '.join(a.tools)}{'；只读' if a.read_only else ''}）"
                for a in self.agents.values()
            )
            self.description = (  # type: ignore[misc]
                f"{type(self).description}\n可以用 agent 参数派专门的角色（不写就是通用的只读调研员）：\n{listing}\n"
                "带改文件 / 跑命令工具的角色会按权限模式向用户确认，而且不并行。"
            )
            self.input_schema = {  # type: ignore[misc]
                **type(self).input_schema,
                "properties": {
                    **type(self).input_schema["properties"],
                    "agent": {"type": "string", "enum": list(self.agents), "description": "派哪个角色（可省）"},
                },
            }

    def summary(self, args: dict[str, Any]) -> str:
        agent = args.get("agent")
        return f"{agent}：{args.get('description', '')}" if agent else str(args.get("description", ""))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("agent") or "")
        role = self.agents.get(name) if name else None
        if name and role is None:
            return ToolResult(f"没有叫 {name} 的角色，有这些：{', '.join(self.agents) or '无'}", is_error=True)
        sub, approve = self.spawn(role)
        text: list[str] = []

        def on_event(event: Any) -> None:
            if isinstance(event, TextDelta):
                text.append(event.text)

        intro = f"{role.prompt}\n\n任务：\n" if role else SUBAGENT_PROMPT
        done: TurnDone = sub.run(intro + str(args["prompt"]), on_event, approve)
        answer = "".join(text).strip() or "（子智能体没有给出结论）"
        if len(answer) > MAX_RESULT_CHARS:
            answer = answer[:MAX_RESULT_CHARS] + "\n…（结论太长，后面截掉了）"
        if self.on_usage is not None:
            self.on_usage(done.usage)
        return ToolResult(f"{answer}\n\n（子智能体用了 {done.rounds} 轮）")

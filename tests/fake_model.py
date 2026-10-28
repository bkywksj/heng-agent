"""脚本化的假模型：实现与 AnthropicClient 相同的 stream 接口，按预设脚本逐轮回复。

每一轮是一个「步」列表：字符串 = 说一段话，("tool", 名字, 参数) = 调一次工具，("think", 内容) = 一段思考
（吐出思考文字和一个带签名的完整思考块，和 Anthropic 的流一样）。
每次被调用时把收到的 messages 拷一份记下来，测试可以检查「工具结果有没有喂回给模型」。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

from heng_agent.core.types import Message, ModelEvent, TextDelta, ThinkingBlock, ThinkingDelta, ToolCall, TurnEnd

Step = str | tuple[str, str, dict[str, Any]] | tuple[str, str]


class FakeModel:
    def __init__(self, script: list[list[Step]]) -> None:
        self.script = list(script)
        self.calls: list[list[Message]] = []
        self.seen_tools: list[str] = []
        self.seen_specs: list[list[dict[str, Any]]] = []  # 每次请求带的工具声明（按需加载的测试要看）
        self.thinking_budget = 0  # 智能体每次请求前会设（和真客户端一样）
        self.seen_budgets: list[int] = []
        self.seen_systems: list[str] = []  # 每次请求的系统提示词（权限模式等会随轮次变）

    def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> Iterator[ModelEvent]:
        self.calls.append(copy.deepcopy(messages))
        self.seen_tools = [t["name"] for t in tools]
        self.seen_specs.append(list(tools))
        self.seen_budgets.append(self.thinking_budget)
        self.seen_systems.append(system)
        if not self.script:
            raise AssertionError("假模型的脚本用完了：循环比预期多调了一轮模型")
        steps = self.script.pop(0)
        n_calls = 0
        for step in steps:
            if isinstance(step, str):
                yield TextDelta(step)
            elif step[0] == "think":
                yield ThinkingDelta(step[1])
                yield ThinkingBlock({"type": "thinking", "thinking": step[1], "signature": f"sig-{len(self.calls)}"})
            else:
                _, name, args = step
                n_calls += 1
                yield ToolCall(id=f"toolu_{len(self.calls)}_{n_calls}", name=name, input=args)
        yield TurnEnd("tool_use" if n_calls else "end_turn", {"input_tokens": 10, "output_tokens": 5})

"""/btw 旁支问题：回答过程中（或平时）随口问一句，不打断正在跑的这一问、不进对话历史（照 Claude Code 的 /btw）。

- 看得到到目前为止的对话（提问、回复、工具结果），看不到正在写的那段回复；也带上之前问过的旁支问题（最近 20 组）；
- 不执行工具：模型只能直接回答。但请求里照样带着主对话的工具声明（只在要求里写「不要调用工具」）：
  提示缓存按 工具 → 系统提示词 → 消息 的顺序比前缀，少了工具声明后面整段对话都对不上、按全价算；
  思考设置也和主对话一样（由调用方设好客户端）。模型还是去调了工具的话，工具不执行，回答后面注明一句；
- 用单独建的模型客户端：正在跑的那一问还在用原来的客户端，共用的话 ESC 打断主回答会把旁支问题也取消掉；
- 答案打印在一个框里（不进对话历史）。Claude Code 是可以关掉的浮层，终端里我们先打印出来。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from heng_agent.core.types import Message, TextDelta, ToolCall

MAX_HISTORY = 20  # 带上之前多少组旁支问答
INSTRUCTION = (
    "（这是一个旁支问题：用户在你工作的间隙随口问的。直接用文字简短回答，不要调用任何工具，"
    "也不要接着做之前的任务；回答不会进入对话历史。）\n\n"
)


def snapshot(messages: list[Message]) -> list[Message]:
    """对话的一份拷贝，去掉末尾没配对的工具调用：回答进行到一半时，最后一条可能是还没拿到结果的 tool_use，
    原样发出去服务会拒收（tool_use 后面必须紧跟 tool_result）。"""
    msgs = copy.deepcopy(messages)
    while msgs and msgs[-1].get("role") == "assistant":
        content = msgs[-1].get("content")
        if isinstance(content, list) and any(b.get("type") == "tool_use" for b in content):
            msgs.pop()
            continue
        break
    return msgs


def build(messages: list[Message], history: list[tuple[str, str]], question: str) -> list[Message]:
    """发给模型的消息：对话快照 + 之前的旁支问答 + 这一问。旁支问答当成普通的一问一答接在后面。"""
    msgs = snapshot(messages)
    for asked, answered in history[-MAX_HISTORY:]:
        msgs += [{"role": "user", "content": [{"type": "text", "text": INSTRUCTION + asked}]},
                 {"role": "assistant", "content": [{"type": "text", "text": answered}]}]  # fmt: skip
    msgs.append({"role": "user", "content": [{"type": "text", "text": INSTRUCTION + question}]})
    return msgs


TOOL_NOTE = "（模型想调用工具接着查，旁支问题里不执行工具；要查的话请在主对话里问）"


def ask(client: Any, system: str, messages: list[Message], history: list[tuple[str, str]], question: str,
        on_text: Callable[[str], None] | None = None, tools: list[dict[str, Any]] | None = None) -> str:  # fmt: skip
    """问一次，返回回答的文字。会发网络请求、可能要几秒：别在界面线程里调。

    tools 传主对话这一问的工具声明（agent.tool_specs()），只为了和主对话前缀一致、吃到缓存，不执行。"""
    parts: list[str] = []
    called = False
    for event in client.stream(system, build(messages, history, question), tools or []):
        if isinstance(event, TextDelta):
            parts.append(event.text)
            if on_text is not None:
                on_text(event.text)
        elif isinstance(event, ToolCall):
            called = True
    answer = "".join(parts).strip()
    return f"{answer}\n\n{TOOL_NOTE}".strip() if called else answer

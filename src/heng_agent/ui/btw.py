"""/btw 旁支问题：回答过程中（或平时）随口问一句，不打断正在跑的这一问、不进对话历史（照 Claude Code 的 /btw）。

- 看得到到目前为止的对话（提问、回复、工具结果），看不到正在写的那段回复；也带上之前问过的旁支问题（最近 20 组）；
- 不给工具：模型只能直接回答；
- 用单独建的模型客户端：正在跑的那一问还在用原来的客户端，共用的话 ESC 打断主回答会把旁支问题也取消掉；
- 答案打印在一个框里（不进对话历史）。Claude Code 是可以关掉的浮层，终端里我们先打印出来。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from heng_agent.core.types import Message, TextDelta

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


def ask(client: Any, system: str, messages: list[Message], history: list[tuple[str, str]], question: str,
        on_text: Callable[[str], None] | None = None) -> str:  # fmt: skip
    """问一次，返回回答的文字（不带工具）。会发网络请求、可能要几秒：别在界面线程里调。"""
    parts: list[str] = []
    for event in client.stream(system, build(messages, history, question), []):
        if isinstance(event, TextDelta):
            parts.append(event.text)
            if on_text is not None:
                on_text(event.text)
    return "".join(parts).strip()

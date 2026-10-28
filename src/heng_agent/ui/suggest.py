"""下一句建议：每一问答完后，猜用户接下来最可能说的一句话，灰字显示在空的输入框里，Tab 采纳（照 Claude Code）。

- 在后台线程里问，用单独建的模型客户端（不和下一问抢同一个连接）；不带工具，只要一句短话；
- 没有明显的下一步就不给，宁缺毋滥：给了一句不着边的，比不给更打扰；
- 每一问多一次请求、带着整段对话：Anthropic 协议大多命中提示缓存（0.1 倍计价），别的协议没有缓存会贵一些，
  所以有开关（设置 prompt_suggestions）。
"""

from __future__ import annotations

from typing import Any

from heng_agent.core.types import Message, TextDelta
from heng_agent.ui import btw

# 不写具体的例句：写了「运行测试看看」，模型就原样照抄，和对话毫无关系
# （真实终端里实测：问版本号，建议是「运行测试看看」）
INSTRUCTION = (
    "（这不是用户说的话，是系统在问你：）根据上面的对话，预测用户接下来最可能输入的一句话。"
    "依据你刚才的回复：你提了下一步、问了用户问题或者给了几个选项，就写出用户最可能的回应；"
    "用户之前的话里说了接下来要做的事，就写那件事。站在用户的角度、用用户的语气，具体到这次对话里的东西。"
    "只输出这一句，不超过 30 个字，不要引号、不要解释、不要调用工具。没有明显的下一步就什么都不输出。"
)
MAX_CHARS = 60  # 超过这么长的不要（多半是模型没听话，在解释）


def build(messages: list[Message]) -> list[Message] | None:
    """发给模型的消息：对话快照 + 要求。上一条不是助手的回复（这一问出错、被打断）就不问。"""
    msgs = btw.snapshot(messages)
    if not msgs or msgs[-1].get("role") != "assistant":
        return None
    return [*msgs, {"role": "user", "content": [{"type": "text", "text": INSTRUCTION}]}]


def clean(text: str) -> str:
    """把模型的回答整理成一句建议；不像一句建议的（空、太长、多行、括号里的说明）返回空串。"""
    line = text.strip().strip("\"'“”「」『』").strip()
    if not line or "\n" in line or len(line) > MAX_CHARS or line.startswith(("（", "(")):
        return ""
    return line


def ask(client: Any, system: str, messages: list[Message]) -> str:
    """问一次下一句建议；没有就返回空串。会发网络请求，别在界面线程里调。"""
    request = build(messages)
    if request is None:
        return ""
    parts = [e.text for e in client.stream(system, request, []) if isinstance(e, TextDelta)]
    return clean("".join(parts))

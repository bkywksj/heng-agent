"""数据类型：模型事件、智能体事件、工具结果。

消息本身直接用 Anthropic Messages 协议的字典格式（`{"role": ..., "content": [...]}`），
原样发给模型、原样存进会话文件，不再定义一套中间格式——少一层转换就少一类 bug。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]


# ---------- 模型客户端吐出的事件（已从 SSE 归一） ----------


@dataclass(frozen=True)
class TextDelta:
    """模型输出的一段文字。"""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """一次完整的工具调用：input_json_delta 已经拼完并解析成字典。"""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class TurnEnd:
    """模型这一轮说完了。stop_reason 是 "tool_use" 时循环要执行工具再继续。"""

    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ThinkingDelta:
    """模型思考过程的一段文字（只给界面显示）。"""

    text: str


@dataclass(frozen=True)
class ThinkingBlock:
    """一个完整的思考块（Anthropic 的 thinking / redacted_thinking，带签名）。

    要原样存进对话历史：调用工具时，下一次请求必须把它们一字不差地传回，否则服务拒收。
    """

    block: dict[str, Any]


ModelEvent = TextDelta | ToolCall | TurnEnd | ThinkingDelta | ThinkingBlock


# ---------- 工具 ----------


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。content 原样交给模型；is_error 让模型知道出错了、该换路子。"""

    content: str
    is_error: bool = False


# ---------- 智能体交给界面的事件 ----------


@dataclass(frozen=True)
class ToolStarted:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolFinished:
    id: str
    name: str
    result: ToolResult
    denied: bool = False


@dataclass(frozen=True)
class TurnDone:
    """用户这一问彻底处理完（模型不再调工具）。usage 是这一问内各次模型调用的合计。"""

    usage: dict[str, int]
    rounds: int


@dataclass(frozen=True)
class Notice:
    """给用户看的过程提示（自动压缩、重试等），不进对话历史。"""

    text: str


AgentEvent = TextDelta | ToolStarted | ToolFinished | TurnDone | Notice | ThinkingDelta

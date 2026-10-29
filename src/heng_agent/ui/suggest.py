"""下一句建议：每一问答完后，猜用户接下来最可能说的一句话，灰字显示在空的输入框里，Tab 采纳（照 Claude Code）。

- 在后台线程里问，用单独建的模型客户端（不和下一问抢同一个连接）；只要一句短话；
- 没有明显的下一步就不给，宁缺毋滥：给了一句不着边的，比不给更打扰；
- 每一问多一次请求，按主对话上一次请求**实际有没有读写缓存**分两种发法（见 plan）。不按协议猜：
  Anthropic 协议的中转站有的根本不缓存（实测 yongye 读写都是 0），OpenAI 协议的 DeepSeek、OpenAI、通义反而会自动缓存；
  - 有缓存：和主对话的请求**前缀一模一样**（同样的系统提示词、工具声明、思考设置、整段对话），只在末尾多一条要求，
    前面整段读主对话刚写下的缓存（Claude Code 也是这么做的）。
    工具不能靠拿掉来禁用：缓存按 工具 → 系统提示词 → 消息 的顺序比前缀，少了工具声明后面全部对不上、按全价算；
  - 没缓存：只带最近几问、压成一段文字，配一句短的系统提示词，每次几千 token 以内，不随对话变长而上涨。
    实测 DeepSeek（OpenAI 协议）：照前缀发 读缓存 14848、新输入 212、输出 11；只带最近几问 新输入 281，
    但模型先推理了几百 token（输出 441～721）——有缓存时照前缀发反而更省，也看得到整段对话。
- 每次的用量记进日志（读了多少缓存、新算了多少输入），命中没命中看日志就知道。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from heng_agent.core import logs
from heng_agent.core.types import Message, TextDelta, ToolCall, TurnEnd
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

#: 不吃缓存时的系统提示词：主对话的系统提示词动辄上万 token（项目说明、技能清单……），猜一句话用不着
BRIEF_SYSTEM = "你在帮一个终端编码助手的用户预测他接下来要输入的话。只按要求输出一句，不要别的。"
BRIEF_TURNS = 3  # 不吃缓存时带最近几问
BRIEF_CHARS = 6000  # 压成的那段文字最多多少字（超了从前面截掉：越近的越要紧）
BRIEF_PART = 800  # 其中每段（一条回复、一个工具结果）最多多少字


@dataclass
class Plan:
    """一次建议请求怎么发：system、messages、tools 原样交给 client.stream。"""

    system: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    cached: bool = False  # 是不是照着主对话的前缀发（吃缓存）


def plan(system: str, messages: list[Message], tools: list[dict[str, Any]], cache_works: bool) -> Plan | None:
    """挑发法；上一条不是助手的回复（这一问出错、被打断）返回 None：不问。

    system、tools 是主对话这一问用的（agent.system_prompt()、agent.tool_specs()），吃缓存时原样带上；
    cache_works：主对话最近一次请求读到或写下了缓存（agent.last_cache_tokens > 0）。"""
    msgs = btw.snapshot(messages)
    if not msgs or msgs[-1].get("role") != "assistant":
        return None
    ask_block = {"role": "user", "content": [{"type": "text", "text": INSTRUCTION}]}
    if cache_works:
        return Plan(system, [*msgs, ask_block], tools, cached=True)
    brief = {"role": "user", "content": [{"type": "text", "text": f"{transcript(msgs)}\n\n{INSTRUCTION}"}]}
    return Plan(BRIEF_SYSTEM, [brief])


def transcript(messages: list[Message]) -> str:
    """最近 BRIEF_TURNS 问压成一段文字（不吃缓存时用）：思考不要，工具调用只留名字和参数开头，长的截短。"""
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user" and _said(m)]
    lines: list[str] = []
    for msg in messages[starts[-BRIEF_TURNS] if len(starts) >= BRIEF_TURNS else 0 :]:
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for b in blocks:
            kind = b.get("type")
            if kind == "text" and b.get("text", "").strip():
                who = "用户" if msg.get("role") == "user" else "助手"
                lines.append(f"{who}：{_cut(b['text'])}")
            elif kind == "tool_use":
                lines.append(
                    f"[助手调用工具 {b.get('name')}：{_cut(json.dumps(b.get('input'), ensure_ascii=False), 200)}]"
                )
            elif kind == "tool_result":
                lines.append(f"[工具结果：{_cut(_result_text(b.get('content')), 300)}]")
    text = "\n".join(lines)
    return text[-BRIEF_CHARS:]


def _said(msg: Message) -> bool:
    """用户真说了话（不是只带工具结果的那种 user 消息）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return any(b.get("type") == "text" for b in content or [])


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def _cut(text: str, limit: int = BRIEF_PART) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def clean(text: str) -> str:
    """把模型的回答整理成一句建议；不像一句建议的（空、太长、多行、括号里的说明）返回空串。"""
    line = text.strip().strip("\"'“”「」『』").strip()
    if not line or "\n" in line or len(line) > MAX_CHARS or line.startswith(("（", "(")):
        return ""
    return line


def ask(client: Any, request: Plan) -> str:
    """按 plan 问一次下一句建议；没有就返回空串。会发网络请求，别在界面线程里调。

    带着工具声明时模型偶尔还是会去调工具：这次的结果整个丢掉（工具不执行）。"""
    parts: list[str] = []
    usage: dict[str, int] = {}
    called = False
    for event in client.stream(request.system, request.messages, request.tools):
        if isinstance(event, TextDelta):
            parts.append(event.text)
        elif isinstance(event, ToolCall):
            called = True
        elif isinstance(event, TurnEnd):
            usage = event.usage
    logs.log.info("下一句建议（%s）：读缓存 %d、写缓存 %d、新输入 %d、输出 %d token%s",
                  "照主对话前缀" if request.cached else "只带最近几问", usage.get("cache_read_tokens", 0),
                  usage.get("cache_write_tokens", 0), usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                  "；模型去调了工具，丢掉" if called else "")  # fmt: skip
    return "" if called else clean("".join(parts))

"""上下文压缩：对话历史快撑满模型上下文时，把旧历史交给模型总结，用摘要替换，只保留正在进行的末尾。

压缩后的历史：
    user  「之前的对话已压缩……摘要」
    （保留的末尾）

末尾怎么取：最后一条是工具结果时，必须把「带工具调用的助手消息 + 工具结果」成对保留——
工具结果引用了调用的 id，调用被压掉的话模型服务会拒收。最后一条是用户的新问题时只留它，并在摘要后补一句助手确认，
保证 user / assistant 交替。
"""

from __future__ import annotations

import json
from typing import Any

from heng_agent.core.types import Message, TextDelta
from heng_agent.model.client import ModelClient

SUMMARY_SYSTEM = "你负责压缩一段编码智能体与用户的工作对话，让智能体只看摘要就能无缝接着干。用中文写，只输出摘要本身。"
SUMMARY_REQUEST = """下面是之前的对话记录。写一份工作摘要，包括：
1. 用户要做什么（原话里的关键要求、约束、不要做的事）；
2. 已经做了什么：读过、改过、新建了哪些文件，改了什么；跑过哪些命令，结果如何；
3. 做出的决定与原因、发现的问题；
4. 还没完成的事、下一步打算。
文件路径、函数名、报错信息要原样保留。

对话记录：
"""
MAX_TOOL_TEXT = 1500  # 单条工具结果 / 参数在摘要输入里最多保留多少字符


def _clip(text: str, limit: int = MAX_TOOL_TEXT) -> str:
    return text if len(text) <= limit else text[:limit] + f"…（截掉 {len(text) - limit} 字）"


def transcript(messages: list[Message], max_chars: int) -> str:
    """把历史渲染成纯文本（不带工具调用格式：有的服务要求带工具调用的历史必须同时声明工具）。

    太长时丢最早的部分：越近的越重要。
    """
    lines: list[str] = []
    for msg in messages:
        content = msg["content"]
        blocks: list[dict[str, Any]] = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for b in blocks:
            if b.get("type") == "text" and b.get("text"):
                lines.append(f"【{'用户' if msg['role'] == 'user' else '助手'}】{b['text']}")
            elif b.get("type") == "tool_use":
                args = _clip(json.dumps(b.get("input", {}), ensure_ascii=False))
                lines.append(f"【助手调用工具 {b.get('name')}】{args}")
            elif b.get("type") == "tool_result":
                flag = "（出错）" if b.get("is_error") else ""
                lines.append(f"【工具结果{flag}】{_clip(str(b.get('content', '')))}")
            elif b.get("type") == "image":
                lines.append("【用户附图】（图片内容不进摘要）")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else "…（更早的对话太长，已省略）\n" + text[-max_chars:]


def split_tail(messages: list[Message]) -> int:
    """要保留的末尾从第几条开始。"""
    last = messages[-1]
    content = last["content"]
    has_results = isinstance(content, list) and any(b.get("type") == "tool_result" for b in content)
    if has_results and len(messages) >= 2 and messages[-2]["role"] == "assistant":
        return len(messages) - 2  # 工具调用与结果成对保留
    return len(messages) - 1


def compact(
    client: ModelClient, messages: list[Message], max_chars: int, instruction: str = ""
) -> tuple[list[Message], str]:
    """返回 (压缩后的历史, 摘要)。历史太短没什么可压时原样返回、摘要为空。

    instruction：用户对摘要的要求（/compact 后面写的话），如「只保留和数据库迁移有关的内容」。
    """
    if not messages:
        return messages, ""
    start = split_tail(messages)
    if start <= 0:
        return messages, ""
    extra = f"\n\n用户对这份摘要的额外要求（优先满足）：{instruction.strip()}\n" if instruction.strip() else ""
    request = [
        {
            "role": "user",
            "content": [{"type": "text", "text": SUMMARY_REQUEST + extra + transcript(messages[:start], max_chars)}],
        }
    ]
    summary = "".join(e.text for e in client.stream(SUMMARY_SYSTEM, request, []) if isinstance(e, TextDelta)).strip()
    head: list[Message] = [
        {"role": "user", "content": [{"type": "text", "text": f"【之前的对话已压缩，以下是工作摘要】\n\n{summary}"}]}
    ]
    tail = messages[start:]
    if tail[0]["role"] == "user":  # 末尾是用户的新问题：补一句助手确认，保持 user / assistant 交替
        head.append({"role": "assistant", "content": [{"type": "text", "text": "好的，我已了解之前的进展，接着做。"}]})
    return head + tail, summary

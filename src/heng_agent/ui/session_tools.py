"""会话工具：/copy 复制回复、/export 导出 Markdown、/context 上下文构成、/diff 本会话的文件改动。"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from heng_agent.core.types import Message

IMAGE_TOKENS = 1600  # 一张图大约占的 token（Anthropic 的估算：约 宽×高/750，常见截图在一两千）
DEFAULT_CHARS_PER_TOKEN = 2.5  # 还没有真实用量时的粗估：中文约 1.5 字一个 token，英文与代码约 4 字符


def text_of(message: Message) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def last_reply(messages: list[Message]) -> str:
    """最近一条有文字的助手回复。"""
    for m in reversed(messages):
        if m.get("role") == "assistant" and text_of(m).strip():
            return text_of(m).strip()
    return ""


# ---------- /copy ----------


def copy_text(text: str) -> tuple[bool, str]:
    """把文字放进剪贴板。返回 (成功, 说明)。"""
    try:
        if sys.platform == "win32":
            # clip 读 UTF-16 才不会把中文弄成乱码（Python 的 utf-16 编码自带 BOM，clip 认得）
            subprocess.run(["clip"], input=text.encode("utf-16"), check=True, timeout=10)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=10)
        else:
            tool = next((t for t in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"])
                         if shutil.which(t[0])), None)  # fmt: skip
            if tool is None:
                return False, "找不到剪贴板工具（装 wl-copy、xclip 或 xsel 其中之一）"
            subprocess.run(tool, input=text.encode("utf-8"), check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"复制失败：{exc}"
    return True, f"已复制（{len(text)} 字）"


# ---------- /export ----------


def export_markdown(messages: list[Message], title: str = "", result_chars: int = 2000) -> str:
    """对话导出成 Markdown：提问、回复照录，工具调用列出参数，工具结果截断。"""
    lines = [f"# {title or 'heng-agent 会话'}", "", f"导出时间：{_dt.datetime.now():%Y-%m-%d %H:%M}", ""]
    for m in messages:
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        if m.get("role") == "user" and any(b.get("type") == "text" for b in blocks):
            lines += ["## 🧑 提问", ""]
        elif m.get("role") == "assistant":
            lines += ["## 🤖 回复", ""]
        for b in blocks:
            kind = b.get("type")
            if kind == "text" and b.get("text", "").strip():
                lines += [b["text"].strip(), ""]
            elif kind == "tool_use":
                args = json.dumps(b.get("input", {}), ensure_ascii=False, indent=1)
                lines += [f"**调用工具 `{b.get('name')}`**", "", "```json", args, "```", ""]
            elif kind == "tool_result":
                body = str(b.get("content", ""))
                if len(body) > result_chars:
                    body = body[:result_chars] + f"\n…（共 {len(body)} 字，后面省略）"
                flag = "（出错）" if b.get("is_error") else ""
                lines += [f"<details><summary>工具结果{flag}</summary>", "", "```", body, "```", "", "</details>", ""]
            elif kind == "image":
                lines += ["*（附了一张图片）*", ""]
    return "\n".join(lines).rstrip() + "\n"


def default_export_path(workdir: Path, session_id: str) -> Path:
    return workdir / ".heng-agent" / "exports" / f"{session_id or 'session'}.md"


# ---------- /context ----------


def context_rows(system: str, tool_specs: list[dict[str, Any]], messages: list[Message],
                 real_total: int) -> list[tuple[str, int]]:  # fmt: skip
    """上下文的构成：(类别, 估算 token)。

    模型只告诉我们总数（real_total）；各部分按字符数比例从总数里分（图片按每张约 IMAGE_TOKENS 另算）。
    还没有真实总数（新会话第一问之前）就按每 token 约 2.5 字符粗估。
    """
    chars = {
        "系统提示词": len(system),
        "工具声明": len(json.dumps(tool_specs, ensure_ascii=False)),
        "你的消息": 0,
        "AI 回复": 0,
        "工具调用与结果": 0,
    }
    images = 0
    for m in messages:
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for b in blocks:
            kind = b.get("type")
            if kind == "text":
                chars["你的消息" if m.get("role") == "user" else "AI 回复"] += len(b.get("text", ""))
            elif kind == "tool_use":
                chars["工具调用与结果"] += len(json.dumps(b.get("input", {}), ensure_ascii=False))
            elif kind == "tool_result":
                chars["工具调用与结果"] += len(str(b.get("content", "")))
            elif kind == "image":
                images += 1
    image_tokens = images * IMAGE_TOKENS
    total_chars = sum(chars.values()) or 1
    text_tokens = max(real_total - image_tokens, 0) if real_total else total_chars / DEFAULT_CHARS_PER_TOKEN
    rows = [(name, round(text_tokens * n / total_chars)) for name, n in chars.items()]
    if images:
        rows.append((f"图片（{images} 张）", image_tokens))
    return rows


# ---------- /diff ----------


def session_diff(originals: dict[str, bytes | None], workdir: Path, max_lines: int = 400) -> list[tuple[str, str]]:
    """本会话改过的文件：每个文件和「第一次改之前」比的统一 diff。返回 [(显示路径, diff)]，没变化的跳过。"""
    out = []
    for key, before in sorted(originals.items()):
        path = Path(key)
        after = path.read_bytes() if path.is_file() else None
        if after == before:
            continue  # 改了又改回去了（或撤销了）
        try:
            shown = path.relative_to(workdir.resolve()).as_posix()
        except ValueError:
            shown = key
        old = (before or b"").decode("utf-8", errors="replace").splitlines()
        new = (after or b"").decode("utf-8", errors="replace").splitlines()
        old_name = f"a/{shown}" if before is not None else "/dev/null"  # 原来不存在 / 被删掉：按 git 的写法
        new_name = f"b/{shown}" if after is not None else "/dev/null"
        diff = list(difflib.unified_diff(old, new, fromfile=old_name, tofile=new_name, lineterm=""))
        if len(diff) > max_lines:
            diff = [*diff[:max_lines], f"…（共 {len(diff)} 行，后面省略）"]
        out.append((shown, "\n".join(diff)))
    return out

"""tool_search：MCP 工具太多时按需加载。

接了几个大的 MCP 服务，工具声明动辄几十万字符、十万 token，每次请求都带上会占掉一半上下文、每问都花这么多钱。
所以工具多时（见 Agent._defer_mcp）先不发 MCP 工具的声明，只给模型这个搜索工具：按关键词在工具名与说明里找，
搜到的立即加载，下一步起就能直接调用（Claude Code 的做法相同）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from heng_agent.core.types import ToolResult
from heng_agent.tools.base import Tool, ToolContext

DEFAULT_RESULTS = 8
MAX_RESULTS = 20


class ToolSearchTool(Tool):
    name = "tool_search"
    kind = "read"
    description = (
        "搜索外部（MCP）工具并加载。外部工具很多，默认没有加载；需要时用关键词搜（中英文都可以，多个词空格分开，"
        "如「截图 窗口」「git push」「deploy server」），搜到的工具立即加载，下一步就能直接调用。"
        "知道确切名字时写 select:名字1,名字2 直接加载。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词，或 select:工具名1,工具名2"},
            "max_results": {"type": "integer", "description": f"最多返回几个（默认 {DEFAULT_RESULTS}）"},
        },
        "required": ["query"],
    }

    def __init__(self, candidates: Callable[[], list[Tool]], load: Callable[[list[str]], None]) -> None:
        """candidates()：可搜的工具（所有 MCP 工具）；load(名字列表)：把它们加进之后请求的工具声明。"""
        self.candidates = candidates
        self.load = load

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("query", ""))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        tools = {t.name: t for t in self.candidates()}
        if not query:
            return ToolResult("query 不能为空", is_error=True)
        if query.startswith("select:"):
            wanted = [n.strip() for n in query[len("select:") :].split(",") if n.strip()]
            found = [tools[n] for n in wanted if n in tools]
            missing = [n for n in wanted if n not in tools]
        else:
            limit = max(1, min(int(args.get("max_results") or DEFAULT_RESULTS), MAX_RESULTS))
            found = search(query, list(tools.values()))[:limit]
            missing = []
        if not found:
            hint = f"没有这些工具：{', '.join(missing)}" if missing else f"没找到和「{query}」相关的工具"
            return ToolResult(f"{hint}。换个说法，或用更短的关键词。")
        self.load([t.name for t in found])
        lines = [f"已加载 {len(found)} 个工具，下一步可以直接调用："]
        lines += [f"- {t.name}：{_first_line(t.description)}" for t in found]
        if missing:
            lines.append(f"没有这些工具：{', '.join(missing)}")
        return ToolResult("\n".join(lines))


def search(query: str, tools: list[Tool]) -> list[Tool]:
    """按关键词打分：每个词出现在工具名里 +3、出现在说明里 +1；只要有一个词对上就算，按分数排。"""
    terms = [t for t in re.split(r"[\s,，、]+", query.lower()) if t]
    scored = []
    for tool in tools:
        name, desc = tool.name.lower(), tool.description.lower()
        score = sum(3 * (term in name) + (term in desc) for term in terms)
        if score:
            scored.append((score, tool.name, tool))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, _, t in scored]


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:150] + ("…" if len(line) > 150 else "")

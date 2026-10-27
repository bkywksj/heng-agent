"""文件工具：读、写、精确替换。

编辑时保持文件原样的两件事（Windows 项目里很常见）：
- 换行风格：文件是 CRLF 就按 CRLF 匹配与写回，不会把整个文件改成 LF；
- BOM：原来有就保留，原来没有就不加。
"""

from __future__ import annotations

import codecs
from typing import Any

from heng_agent.core.errors import AgentError
from heng_agent.core.types import ToolResult
from heng_agent.tools.base import Tool, ToolContext
from heng_agent.tools.paths import display, resolve_in_workdir

MAX_READ_LINES = 2000


def _read_text(path: Any) -> tuple[str, bool]:
    """读成文本，返回 (内容, 是否带 BOM)。换行原样保留（不做 \\r\\n → \\n 的转换）。"""
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        raise AgentError(f"{path.name} 看起来是二进制文件，不按文本处理")
    bom = raw.startswith(codecs.BOM_UTF8)
    try:
        return raw.decode("utf-8-sig"), bom
    except UnicodeDecodeError as exc:
        raise AgentError(f"{path.name} 不是 UTF-8 编码：{exc}") from exc


def _write_text(path: Any, text: str, bom: bool) -> None:
    data = text.encode("utf-8")
    path.write_bytes((codecs.BOM_UTF8 + data) if bom else data)


class ReadFile(Tool):
    name = "read_file"
    description = (
        "读取文本文件，带行号返回（格式：行号<Tab>内容）。大文件用 offset（从 1 起）和 limit 分段读，"
        f"一次最多 {MAX_READ_LINES} 行。改文件前要先读，确认要替换的原文。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对工作目录或绝对路径"},
            "offset": {"type": "integer", "description": "从第几行开始读，默认 1"},
            "limit": {"type": "integer", "description": f"最多读多少行，默认 {MAX_READ_LINES}"},
        },
        "required": ["path"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_in_workdir(args["path"], ctx.workdir)
        if not path.is_file():
            return ToolResult(f"文件不存在：{args['path']}", is_error=True)
        text, _ = _read_text(path)
        lines = text.splitlines()
        start = max(1, int(args.get("offset", 1)))
        limit = min(MAX_READ_LINES, max(1, int(args.get("limit", MAX_READ_LINES))))
        chunk = lines[start - 1 : start - 1 + limit]
        body = "\n".join(f"{start + i}\t{line}" for i, line in enumerate(chunk))
        more = len(lines) - (start - 1 + len(chunk))
        tail = f"\n…（还有 {more} 行，用 offset={start + len(chunk)} 继续读）" if more > 0 else ""
        return ToolResult((body or "（空文件）") + tail)


class WriteFile(Tool):
    name = "write_file"
    kind = "edit"
    description = "新建文件或整体覆盖已有文件。只改局部时用 edit_file，不要整体重写。父目录不存在会自动创建。"
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string", "description": "完整的文件内容"}},
        "required": ["path", "content"],
    }

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')}（{len(str(args.get('content', '')).splitlines())} 行）"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_in_workdir(args["path"], ctx.workdir)
        existed = path.exists()
        bom = existed and path.read_bytes().startswith(codecs.BOM_UTF8)
        ctx.before_write(path)  # 记下原内容（或「原来不存在」），/undo 时恢复
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, args["content"], bom)
        lines = len(args["content"].splitlines())
        return ToolResult(f"{'覆盖' if existed else '新建'} {display(path, ctx.workdir)}（{lines} 行）")


class EditFile(Tool):
    name = "edit_file"
    kind = "edit"
    description = (
        "精确替换文件里的一段文字。old_string 必须与文件内容逐字一致（含缩进），且只出现一次；"
        "要替换所有出现处就设 replace_all=true。先用 read_file 看准原文（不要带行号前缀）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "要被替换的原文"},
            "new_string": {"type": "string", "description": "替换成的新文字"},
            "replace_all": {"type": "boolean", "description": "替换所有出现处，默认 false"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')}：替换 {len(str(args.get('old_string', '')).splitlines()) or 1} 行"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_in_workdir(args["path"], ctx.workdir)
        if not path.is_file():
            return ToolResult(f"文件不存在：{args['path']}", is_error=True)
        old, new = args["old_string"], args["new_string"]
        if old == new:
            return ToolResult("old_string 与 new_string 相同，没有可改的", is_error=True)
        text, bom = _read_text(path)
        if "\r\n" in text and "\r\n" not in old:  # CRLF 文件：模型给的是 \n，按文件的换行风格匹配与写回
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        count = text.count(old)
        if count == 0:
            return ToolResult("没找到 old_string：先用 read_file 看准原文（缩进、空格都要一致）", is_error=True)
        if count > 1 and not args.get("replace_all"):
            return ToolResult(
                f"old_string 出现了 {count} 次：多带几行上下文让它唯一，或设 replace_all=true", is_error=True
            )
        ctx.before_write(path)
        _write_text(path, text.replace(old, new), bom)
        return ToolResult(f"已修改 {display(path, ctx.workdir)}（替换 {count if args.get('replace_all') else 1} 处）")

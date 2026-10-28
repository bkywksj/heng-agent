"""搜索工具：按通配找文件、按正则搜内容。纯 Python 实现，不依赖系统里有没有 rg / grep。

都会跳过版本库、虚拟环境、依赖目录（paths.SKIP_DIRS），并限制返回条数：结果太多会把模型的上下文挤满。
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from heng_agent.core.types import ToolResult
from heng_agent.tools.base import Tool, ToolContext
from heng_agent.tools.paths import SKIP_DIRS, display, is_inside, resolve_in_workdir

MAX_RESULTS = 200
MAX_FILE_BYTES = 2 * 1024 * 1024  # 超过 2 MB 的文件不搜：多半是数据或产物


def walk_files(root: Path, workdir: Path) -> Iterator[Path]:
    """遍历 root 下的文件，只给出解析后仍在工作目录内的。

    os.walk 不跟目录符号链接，但会钻进 Windows 的目录联接（它不算符号链接）；文件符号链接则照样列出来。
    所以逐个检查：逃到外面的目录直接剪掉不进去，逃到外面的文件跳过——否则 grep 能读出工作目录外的文件内容。
    """
    boundary = workdir.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and is_inside(here / d, boundary))
        for name in sorted(filenames):
            if is_inside(here / name, boundary):
                yield here / name


class Glob(Tool):
    name = "glob"
    description = "按通配模式找文件，如 **/*.py、src/**/test_*.py。按修改时间从新到旧，最多返回 200 个。"
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "通配模式，** 匹配任意层目录"},
            "path": {"type": "string", "description": "从哪个目录开始找，默认工作目录"},
        },
        "required": ["pattern"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        base = resolve_in_workdir(args.get("path") or ".", ctx.workdir)
        pattern = args["pattern"]
        hits = []
        for f in walk_files(base, ctx.workdir):
            rel = f.relative_to(base).as_posix()
            # 「**/x」也要匹配根目录下的 x：fnmatch 的 * 不跨 /，所以多试一次去掉前缀的形式
            if fnmatch.fnmatch(rel, pattern) or (pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:])):
                hits.append(f)
        hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        shown = [display(p, ctx.workdir) for p in hits[:MAX_RESULTS]]
        more = f"\n…（共 {len(hits)} 个，只列出前 {MAX_RESULTS} 个）" if len(hits) > MAX_RESULTS else ""
        return ToolResult(("\n".join(shown) or "没有匹配的文件") + more)


class Grep(Tool):
    name = "grep"
    description = "按正则表达式搜索文件内容，返回「文件:行号: 内容」。可用 glob 限定文件类型（如 *.py），最多 200 条。"
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式"},
            "path": {"type": "string", "description": "搜索的目录或文件，默认工作目录"},
            "glob": {"type": "string", "description": "只搜文件名匹配这个通配的文件，如 *.py"},
            "ignore_case": {"type": "boolean"},
        },
        "required": ["pattern"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            regex = re.compile(args["pattern"], re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as exc:
            return ToolResult(f"正则表达式写错了：{exc}", is_error=True)
        base = resolve_in_workdir(args.get("path") or ".", ctx.workdir)
        files = [base] if base.is_file() else walk_files(base, ctx.workdir)
        name_glob = args.get("glob")
        out: list[str] = []
        total = 0
        for f in files:
            if name_glob and not fnmatch.fnmatch(f.name, name_glob):
                continue
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    continue
                raw = f.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue  # 二进制文件
            for no, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    total += 1
                    if len(out) < MAX_RESULTS:
                        out.append(f"{display(f, ctx.workdir)}:{no}: {line.strip()[:200]}")
        more = f"\n…（共 {total} 处，只列出前 {MAX_RESULTS} 处）" if total > MAX_RESULTS else ""
        return ToolResult(("\n".join(out) or "没有匹配") + more)

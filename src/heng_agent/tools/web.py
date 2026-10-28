"""网页读取工具：取一个网页，转成纯文本给模型看（查文档、看报错对应的 issue）。

权限类别是 net：读网页不改本地，但模型可以把代码内容拼进网址带出去，
所以除 auto 外都要用户确认（确认对话里网址完整显示）。
只支持 http / https，响应最多读 2 MB，转出的正文最多 20000 字。
"""

from __future__ import annotations

import html
import re
from typing import Any

import httpx

from heng_agent.core.net import http_client
from heng_agent.core.types import ToolResult
from heng_agent.tools.base import Tool, ToolContext

MAX_BYTES = 2 * 1024 * 1024
MAX_CHARS = 20_000
_DROP = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>", re.S | re.I)
_BLOCK = re.compile(r"</?(p|div|br|li|tr|h[1-6]|pre|section|article|ul|ol|table)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(page: str) -> str:
    text = _DROP.sub(" ", page)
    text = _BLOCK.sub("\n", text)
    text = html.unescape(_TAG.sub("", text))
    lines = (re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class WebFetch(Tool):
    name = "web_fetch"
    kind = "net"
    description = "读取一个网页（http/https），返回去掉标签后的正文，最多 20000 字。用来查官方文档、报错对应的 issue。"
    input_schema = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "完整网址，http:// 或 https:// 开头"}},
        "required": ["url"],
    }

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("url", ""))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args["url"])
        if not url.startswith(("http://", "https://")):
            return ToolResult("只支持 http:// 或 https:// 开头的网址", is_error=True)
        try:
            client = http_client(
                timeout=20.0, follow_redirects=True, headers={"user-agent": "heng-agent"}, proxy=ctx.proxy
            )
            with client, client.stream("GET", url) as resp:
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_BYTES:
                        break
                status, ctype = resp.status_code, resp.headers.get("content-type", "")
                encoding = resp.encoding or "utf-8"
        except httpx.HTTPError as exc:
            return ToolResult(f"取网页失败：{type(exc).__name__}: {exc}", is_error=True)
        body = b"".join(chunks).decode(encoding, errors="replace")
        text = html_to_text(body) if "html" in ctype or body.lstrip()[:1] == "<" else body
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + f"\n…（正文共 {len(text)} 字，只给出前 {MAX_CHARS} 字）"
        return ToolResult(f"HTTP {status}\n{text}", is_error=status >= 400)

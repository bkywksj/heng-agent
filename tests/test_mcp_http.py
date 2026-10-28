"""MCP 的 http 连接（Streamable HTTP）、mcpServers 写法、连接状态的显示格式。"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from heng_agent.core.errors import AgentError
from heng_agent.mcp import MCPManager, ServerConfig, _read_servers, fingerprint

SESSION = "sess-42"


class FakeHttpMCP(http.server.BaseHTTPRequestHandler):
    """假的 Streamable HTTP MCP 服务：握手与列工具回 JSON，调工具回事件流；握手后要求带 Mcp-Session-Id。"""

    deleted: list[str] = []  # noqa: RUF012  测试里用类属性记下服务端收到了什么（每次开头清空）
    seen_protocol: list[str] = []  # noqa: RUF012

    def _send_json(self, obj: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        msg = json.loads(self.rfile.read(int(self.headers["content-length"])))
        method, rid = msg.get("method"), msg.get("id")
        if method != "initialize" and self.headers.get("mcp-session-id") != SESSION:
            self.send_response(400)
            self.end_headers()
            return
        if rid is None:  # 通知
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
            self._send_json({"jsonrpc": "2.0", "id": rid, "result": result}, {"mcp-session-id": SESSION})
        elif method == "tools/list":
            self.seen_protocol.append(self.headers.get("mcp-protocol-version", ""))
            tools = [{"name": "echo", "description": "回声", "inputSchema": {"type": "object", "properties": {}}}]
            self._send_json({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
        elif method == "tools/call":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            noise = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 50}}
            content = [{"type": "text", "text": f"回声：{msg['params']['arguments']['text']}"}]
            answer = {"jsonrpc": "2.0", "id": rid, "result": {"content": content}}
            self.wfile.write(f"event: message\ndata: {json.dumps(noise)}\n\n".encode())  # 先来一条无关的通知
            self.wfile.write(f"data: {json.dumps(answer, ensure_ascii=False)}\n\n".encode())

    def do_DELETE(self) -> None:
        self.deleted.append(self.headers.get("mcp-session-id", ""))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHttpMCP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeHttpMCP.deleted.clear()
    FakeHttpMCP.seen_protocol.clear()
    yield f"http://127.0.0.1:{srv.server_port}/mcp"
    srv.shutdown()


def test_http_mcp_full_flow(server: str, tmp_path: Path) -> None:
    manager = MCPManager([ServerConfig("ai-workstation", url=server, timeout=10)], tmp_path)
    assert manager.status == [("ai-workstation", True, "1 tools (http)")]
    tool = manager.tools[0]
    assert tool.name == "mcp__ai-workstation__echo"
    result = tool.run({"text": "你好"}, None)  # type: ignore[arg-type]
    assert result.content == "回声：你好"  # 事件流里跳过无关通知，取到对应 id 的响应
    assert FakeHttpMCP.seen_protocol == ["2025-06-18"]  # 握手后带上协商出的协议版本
    manager.close()
    assert FakeHttpMCP.deleted == [SESSION]  # 关闭时结束会话


def test_http_mcp_unreachable(tmp_path: Path) -> None:
    manager = MCPManager([ServerConfig("gone", url="http://127.0.0.1:9/mcp", timeout=3)], tmp_path)
    assert manager.status[0][:2] == ("gone", False) and "连不上" in manager.status[0][2]


def test_config_forms(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "mcpServers": {  # Claude Code / Kimi 的写法，直接贴过来能用
            "aicoder": {"command": "aicoder-mcp", "args": ["--stdio"]},
            "ai-workstation": {"type": "http", "url": "http://localhost:9000/mcp",
                               "headers": {"Authorization": "Bearer t"}},
        },
    }), encoding="utf-8")  # fmt: skip
    servers = {s.name: s for s in _read_servers(path, "user")}
    assert servers["aicoder"].transport == "stdio" and servers["ai-workstation"].transport == "http"
    assert servers["ai-workstation"].headers == {"Authorization": "Bearer t"}
    path.write_text(json.dumps({"mcp_servers": {"old": {"type": "sse", "url": "http://x/sse"}}}), encoding="utf-8")
    with pytest.raises(AgentError, match="SSE"):
        _read_servers(path, "user")


def test_fingerprint_covers_url() -> None:
    a = ServerConfig("x", url="http://good.example/mcp")
    b = ServerConfig("x", url="http://evil.example/mcp")
    assert fingerprint([a]) != fingerprint([b])  # 项目配置换了地址：信任失效，要重新确认


def test_status_display_format(tmp_path: Path) -> None:
    from fake_model import FakeModel
    from heng_agent.agent import Agent
    from heng_agent.core.config import Settings
    from heng_agent.ui.repl import Repl

    console = Console(record=True, width=120, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([])), console=console)
    repl.print_mcp_status([("ai-workstation", True, "3 tools (http)"), ("aicoder", True, "90 tools (stdio)"),
                           ("broken", False, "连不上")])  # fmt: skip
    assert console.export_text().splitlines() == [
        'MCP 服务 "ai-workstation" 已连接 · 3 tools (http)',
        'MCP 服务 "aicoder" 已连接 · 90 tools (stdio)',
        'MCP 服务 "broken" 连接失败：连不上',
    ]

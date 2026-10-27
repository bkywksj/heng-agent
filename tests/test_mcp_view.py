"""/mcp 逐级查看：服务总览 → 某服务的工具 → 单个工具的详情。"""

from __future__ import annotations

from pathlib import Path

from rich.cells import cell_len
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.mcp import MCPManager, MCPTool, ServerConfig
from heng_agent.tools.base import builtin_tools
from heng_agent.ui.repl import Repl


class FakeClient:
    def __init__(self, name: str, url: str = "") -> None:
        self.config = ServerConfig(name, command="" if url else "x.exe", url=url)


def _tool(server: str, name: str, description: str, props: dict | None = None) -> MCPTool:  # type: ignore[type-arg]
    schema = {"type": "object", "properties": props or {}, "required": list((props or {}).keys())[:1]}
    return MCPTool(FakeClient(server), {"name": name, "description": description, "inputSchema": schema})  # type: ignore[arg-type]


def _repl(tmp_path: Path) -> tuple[Repl, Console]:
    tools = [
        *builtin_tools(),
        _tool("agileshot", "background_click", "Click in the background desktop. Two modes:\n(1) PREFER ...\n(2) ..."),
        _tool("agileshot", "count_shots", "Return total number of screenshots in local history."),
        _tool(
            "sigil",
            "git_push",
            "推送到远程仓库",
            {"repo_path": {"type": "string", "description": "仓库路径\n第二行"}, "branch": {"type": "string"}},
        ),
    ]
    console = Console(record=True, width=100, force_terminal=False)
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([]), tools=tools)
    repl = Repl(agent, console=console, mcp_status=[("agileshot", True, "2 tools (stdio)"),
                                                     ("sigil", True, "1 tools (stdio)"),
                                                     ("broken", False, "连不上 MCP 服务 broken")])  # fmt: skip
    return repl, console


def test_summary_lists_servers_not_tools(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    repl.handle_command("/mcp")
    lines = console.export_text().splitlines()
    assert any("● agileshot" in ln and "2 个工具" in ln and "stdio" in ln for ln in lines)
    assert any("● sigil" in ln and "1 个工具" in ln for ln in lines)
    assert any("✗ broken" in ln and "连接失败" in ln for ln in lines)  # 连不上的也列出来，带原因
    assert not any("background_click" in ln for ln in lines)  # 总览不列工具


def test_server_tools_one_line_each(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    repl.agent.loaded_mcp.append("mcp__agileshot__count_shots")
    repl.handle_command("/mcp agileshot")
    lines = console.export_text().splitlines()
    click = [ln for ln in lines if "background_click" in ln]
    assert len(click) == 1 and "Two modes" in click[0]  # 说明只取第一行
    assert not any("PREFER" in ln for ln in lines)  # 说明里的换行不再把排版挤乱
    assert any(ln.strip().startswith("✓ count_shots") for ln in lines)  # 已加载的打 ✓
    assert all(len(ln) <= 100 for ln in lines)


def test_tool_detail_and_hints(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    repl.handle_command("/mcp sigil git_push")
    text = console.export_text()
    assert "推送到远程仓库" in text and "repo_path" in text and "必填" in text and "可选" in text
    lines = text.splitlines()
    first = next(ln for ln in lines if "仓库路径" in ln)
    second = next(ln for ln in lines if "第二行" in ln)
    # 比的是屏幕上的列（中文一个字占两格），不是字符个数
    assert cell_len(second[: second.index("第二行")]) == cell_len(
        first[: first.index("仓库路径")]
    )  # 换行后和说明栏对齐
    assert [v for v, _, _ in repl._arg_hints("mcp", [])] == [
        "reconnect",
        "disable",
        "enable",
        "agileshot",
        "sigil",
        "broken",
    ]
    assert [v for v, _, _ in repl._arg_hints("mcp", ["sigil"])] == ["git_push"]
    repl.handle_command("/mcp broken")
    repl.handle_command("/mcp nope")
    text = console.export_text()
    assert "连接失败" in text and "没有叫 nope 的 MCP 服务" in text


# ---------- 重连、停用、启用 ----------


def _live_repl(tmp_path: Path) -> tuple[Repl, Console, MCPManager]:
    import sys

    from heng_agent.mcp import MCPManager

    server = ServerConfig("calc", sys.executable, [str(Path(__file__).parent / "fake_mcp_server.py")], timeout=20)
    manager = MCPManager([server], tmp_path)
    console = Console(record=True, width=120, force_terminal=False)
    agent = Agent(
        Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([]), tools=[*builtin_tools(), *manager.tools]
    )
    repl = Repl(agent, console=console, mcp_status=manager.status)
    repl.mcp_managers.append(manager)
    return repl, console, manager


def test_reconnect_after_crash(tmp_path: Path) -> None:
    repl, console, manager = _live_repl(tmp_path)
    try:
        old = manager.clients["calc"]
        old.proc.kill()  # type: ignore[attr-defined]  # 服务崩了
        old.proc.wait()  # type: ignore[attr-defined]
        broken = repl.agent.tools["mcp__calc__add"].run({"a": 1, "b": 2}, None)  # type: ignore[arg-type]
        assert broken.is_error and "/mcp reconnect calc" in broken.content  # 出错时提示怎么重连
        repl.handle_command("/mcp reconnect calc")
        new = manager.clients["calc"]
        assert new is not old and new.proc.poll() is None  # type: ignore[attr-defined]  # 起了新进程
        fixed = repl.agent.tools["mcp__calc__add"].run({"a": 40, "b": 2}, None)  # type: ignore[arg-type]
        assert fixed.content == "42"  # 智能体里的工具已换成连着新进程的
        assert 'MCP server "calc" connected' in console.export_text()
    finally:
        manager.close()


def test_disable_and_enable(tmp_path: Path) -> None:
    repl, console, manager = _live_repl(tmp_path)
    try:
        client = manager.clients["calc"]
        repl.handle_command("/mcp disable calc")
        assert not any(n.startswith("mcp__calc__") for n in repl.agent.tools)  # 模型看不到它的工具了
        assert client.proc.poll() is not None  # type: ignore[attr-defined]  # 进程关了
        assert [v for v, _, _ in repl._arg_hints("mcp", ["enable"])] == ["calc"]  # enable 只提示停用的
        assert repl._arg_hints("mcp", ["disable"]) == []
        repl.handle_command("/mcp")
        shown = console.export_text()  # export_text 默认会清空已记录的内容：只取一次
        assert "○ calc" in shown and "已停用" in shown
        repl.handle_command("/mcp enable calc")
        assert repl.agent.tools["mcp__calc__add"].run({"a": 1, "b": 1}, None).content == "2"  # type: ignore[arg-type]
        assert "calc" not in repl.mcp_disabled
        repl.handle_command("/mcp reconnect nope")
        assert "没有叫 nope 的 MCP 服务" in console.export_text()
    finally:
        manager.close()

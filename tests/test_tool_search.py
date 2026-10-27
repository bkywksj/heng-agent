"""MCP 工具按需加载（tool_search）：工具多时先不发声明，搜到再加载。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import ToolResult
from heng_agent.tools.base import Tool, ToolContext, builtin_tools
from heng_agent.tools.tool_search import search


class FakeMCP(Tool):
    kind = "mcp"

    def __init__(self, server: str, name: str, description: str) -> None:
        self.name = f"mcp__{server}__{name}"  # type: ignore[misc]
        self.description = description  # type: ignore[misc]
        self.input_schema = {"type": "object", "properties": {"x": {"type": "string", "description": "说明" * 200}}}  # type: ignore[misc]

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(f"{self.name} 执行了")


def _mcp_tools() -> list[Tool]:
    tools: list[Tool] = [FakeMCP("agileshot", "capture_window", "截取指定窗口的截图"),
                         FakeMCP("reeve", "deploy_project", "把项目部署到服务器")]  # fmt: skip
    tools += [FakeMCP("sigil", f"tool_{i}", f"凭据相关的第 {i} 个工具") for i in range(40)]
    return tools


def _names(specs: list[dict[str, Any]]) -> set[str]:
    return {s["name"] for s in specs}


def test_search_scoring() -> None:
    tools = _mcp_tools()
    assert search("截图", tools)[0].name == "mcp__agileshot__capture_window"
    assert search("deploy 服务器", tools)[0].name == "mcp__reeve__deploy_project"  # 名字里有 +3，说明里有 +1
    assert search("完全不相干", tools) == []


def test_deferred_loading_full_flow(tmp_path: Path) -> None:
    model = FakeModel([
        [("tool", "tool_search", {"query": "截图"})],
        [("tool", "mcp__agileshot__capture_window", {"x": "记事本"})],
        ["截好了"],
    ])  # fmt: skip
    settings = Settings(workdir=tmp_path, home=tmp_path / "h", permission_mode="auto", context_window=20_000)
    agent = Agent(settings, model, tools=[*builtin_tools(), *_mcp_tools()])
    assert agent.defer_mcp()  # 42 个大工具超过上下文 10%：按需加载
    agent.run("帮我截一下记事本窗口", lambda e: None, lambda t, a: "once")
    first, second = (_names(specs) for specs in model.seen_specs[:2])
    assert "tool_search" in first and not any(n.startswith("mcp__") for n in first)  # 第一次：MCP 工具一个都不带
    assert "mcp__agileshot__capture_window" in second and "mcp__reeve__deploy_project" not in second  # 只加载搜到的
    results = model.calls[-1][-1]["content"]
    assert results[0]["content"] == "mcp__agileshot__capture_window 执行了"
    assert "agileshot（1 个）" in agent.system_prompt() and "tool_search" in agent.system_prompt()


def test_select_and_modes(tmp_path: Path) -> None:
    settings = Settings(workdir=tmp_path, home=tmp_path / "h", context_window=20_000, mcp_tool_search="off")
    agent = Agent(settings, FakeModel([]), tools=[*builtin_tools(), *_mcp_tools()])
    specs = _names(agent.tool_specs())
    assert "tool_search" not in specs and "mcp__reeve__deploy_project" in specs  # off：全部照发
    small = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([]),
                  tools=[*builtin_tools(), FakeMCP("calc", "add", "加法")])  # fmt: skip
    assert not small.defer_mcp() and "tool_search" not in _names(small.tool_specs())  # 工具少：不必按需加载
    agent.settings = agent.settings.model_copy(update={"mcp_tool_search": "on"})
    tool = agent.tools["tool_search"]
    out = tool.run({"query": "select:mcp__reeve__deploy_project, mcp__nope"}, None)  # type: ignore[arg-type]
    assert "已加载 1 个" in out.content and "没有这些工具：mcp__nope" in out.content
    assert "mcp__reeve__deploy_project" in _names(agent.tool_specs())

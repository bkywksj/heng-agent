"""查看状态的命令：/status、/usage（/cost）、/stats、/hooks；顺带的修复：/clear 清零用量、/rename 不冲掉按模型的用量、
/mcp 服务只列一次。"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.hooks import Hook
from heng_agent.session import SessionStore
from heng_agent.ui.repl import Repl


@pytest.fixture
def repl_console(tmp_path: Path) -> tuple[Repl, Console]:
    work = tmp_path / "proj"
    work.mkdir()
    agent = Agent(Settings(workdir=work, home=tmp_path / "home"), FakeModel([["好的"], ["好的"]]),
                  session=SessionStore(tmp_path / "home", work))  # fmt: skip
    console = Console(record=True, width=160, force_terminal=False)
    return Repl(agent, console=console, provider_name="yongye"), console


def test_status_and_version_alias(repl_console: tuple[Repl, Console]) -> None:
    repl, console = repl_console
    repl.mcp_status = [("calc", True, "2 tools (stdio)"), ("broken", False, "找不到命令")]
    repl.handle_command("/status")
    repl.handle_command("/version")
    shown = console.export_text()
    for key in ("版本", "会话", "目录", "权限", "上下文", "扩展"):
        assert key in shown, key
    assert "1 个服务 · 2 个工具 · 1 个连不上" in shown  # 按启动时的连接状态算
    assert shown.count("heng-agent ") >= 2  # /version 是 /status 的别名


def test_usage_by_model_and_cost_alias(repl_console: tuple[Repl, Console]) -> None:
    repl, console = repl_console
    a = repl.agent
    a.run("你好", lambda e: None, lambda t, args: "once")
    assert a.usage_by_model == {"未知模型": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
                                             "cache_write_tokens": 0}}  # fmt: skip
    a.usage_by_model["另一个模型"] = {"input_tokens": 1000, "output_tokens": 200}
    repl.handle_command("/cost")  # 别名
    shown = console.export_text()
    assert "本会话" in shown and "另一个模型" in shown and "未知模型" in shown  # 换过模型：分开列
    assert "/stats 看历史统计" in shown


def _session_file(home: Path, project: str, name: str, usage_by_model: dict[str, Any], days_ago: float) -> None:
    folder = home / "sessions" / f"{project}-abc"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    data: dict[str, Any] = {"workdir": f"/x/{project}", "messages": []}
    if usage_by_model:
        data["usage_by_model"] = usage_by_model
        data["usage"] = {}
    else:
        data["usage"] = {"input_tokens": 5000, "output_tokens": 500}  # 旧会话：没记模型
    path.write_text(json.dumps(data), encoding="utf-8")
    stamp = time.time() - days_ago * 86400
    os.utime(path, (stamp, stamp))


def test_stats_across_sessions(repl_console: tuple[Repl, Console]) -> None:
    repl, console = repl_console
    home = repl.agent.settings.home
    _session_file(home, "shop", "s1", {"claude-sonnet-5": {"input_tokens": 100_000, "output_tokens": 10_000}}, 0)
    _session_file(home, "shop", "s2", {"deepseek-v4": {"input_tokens": 50_000, "output_tokens": 5_000}}, 3)
    _session_file(home, "blog", "s3", {"claude-sonnet-5": {"input_tokens": 20_000, "output_tokens": 2_000}}, 20)
    _session_file(home, "old", "s4", {}, 100)  # 旧会话：只有总用量
    repl.handle_command("/stats")
    shown = console.export_text()

    def sessions(period: str) -> str:  # 表格有 │ 分隔：在那一行里找第一个数字（会话数）
        line = next(ln for ln in shown.splitlines() if ln.strip().startswith(period))
        return re.findall(r"\d+", line.replace(period, "", 1))[0]

    assert sessions("今天") == "1" and sessions("全部") == "4"  # 今天 1 个会话，全部 4 个
    assert sessions("近 7 天") == "2" and sessions("近 30 天") == "3"
    assert "shop" in shown and "claude-sonnet-5" in shown  # 近 30 天按项目、按模型排名
    assert "old" not in shown.split("近 30 天按项目")[1].splitlines()[0]  # 100 天前的不进近 30 天排名


def test_hooks_listing(repl_console: tuple[Repl, Console]) -> None:
    repl, console = repl_console
    repl.agent.hooks.hooks = [
        Hook("post_tool", "edit_file|write_file", "ruff format", source="user"),
        Hook("pre_tool", "Bash|PowerShell", 'node "guard.cjs"', source="user", style="claude"),
        Hook("stop", ".*", "node lang.cjs", source="user", style="claude"),
    ]
    repl.untrusted_hooks = [Hook("pre_tool", ".*", "evil", source="project")]
    repl.agent.settings = repl.agent.settings.model_copy(update={"claude_hooks_skip": ["recorder"]})
    repl.handle_command("/hooks")
    shown = console.export_text()
    assert "工具执行前" in shown and "Bash|PowerShell" in shown and "Claude Code · 用户" in shown
    assert "回复结束时" in shown and "ruff format" in shown
    assert "1 个钩子没确认信任" in shown and "recorder" in shown


def test_clear_resets_usage_and_rename_keeps_by_model(repl_console: tuple[Repl, Console]) -> None:
    repl, _ = repl_console
    a = repl.agent
    a.run("你好", lambda e: None, lambda t, args: "once")
    repl.handle_command("/rename 购物车")
    saved = json.loads(a.session.path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert saved["title"] == "购物车" and "usage_by_model" in saved  # 起名时没冲掉按模型的用量
    repl.handle_command("/clear")
    assert sum(a.usage.values()) == 0 and a.usage_by_model == {}  # 新会话从零算：/stats 不会重复算上一个会话


def test_mcp_lists_servers_once(repl_console: tuple[Repl, Console], monkeypatch: pytest.MonkeyPatch) -> None:
    repl, console = repl_console
    repl.mcp_status = [("calc", True, "2 tools (stdio)"), ("broken", False, "找不到命令")]
    seen: list[list[str]] = []
    monkeypatch.setattr(repl, "_choose", lambda title, options, default=0: seen.append(
        [f"{o.label} {o.detail}" for o in options]) or None)  # fmt: skip
    repl.handle_command("/mcp")
    shown = console.export_text()
    assert "broken" not in shown  # 服务不先打一遍：只在选择器里列
    assert any("✗ broken" in row and "连接失败：找不到命令" in row for row in seen[0])
    assert "1 个连不上" in shown  # 汇总行

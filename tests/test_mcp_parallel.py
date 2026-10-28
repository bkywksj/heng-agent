"""MCP 服务同时连、同时关（以前一个接一个：6 个服务启动实测 10 秒、退出 5 秒），且顺序不随谁先连上而变。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from heng_agent.core.errors import AgentError
from heng_agent.mcp import CONNECTING, MCPManager, ServerConfig

DELAY = 0.4  # 每个服务连接 / 关闭各要这么久；三个排队就是 1.2 秒，同时做只要 0.4 秒多一点


class FakeClient:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.closed = False

    def close(self) -> None:
        time.sleep(DELAY)
        self.closed = True


def _configs() -> list[ServerConfig]:
    return [ServerConfig(name, command="unused") for name in ("first", "second", "third")]


@pytest.fixture
def slow_open(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把真正的建连接换成慢的假连接：越靠前的服务越晚连上；third 连不上。返回实际连上的先后顺序。"""
    finished: list[str] = []
    lock = threading.Lock()
    order = {"first": 3, "second": 2, "third": 1}

    def fake_open(self: MCPManager, config: ServerConfig) -> tuple[Any, list[dict[str, Any]]] | AgentError:
        time.sleep(DELAY * order[config.name] / 3)
        with lock:
            finished.append(config.name)
        if config.name == "third":
            return AgentError("third 起不来")
        return FakeClient(config), [{"name": f"{config.name}_tool"}]

    monkeypatch.setattr(MCPManager, "_open", fake_open)
    return finished


def test_servers_connect_at_the_same_time(tmp_path: Path, slow_open: list[str]) -> None:
    start = time.perf_counter()
    manager = MCPManager(_configs(), tmp_path)
    elapsed = time.perf_counter() - start
    assert elapsed < DELAY * 2  # 排队要 0.4 + 0.27 + 0.13 = 0.8 秒；同时连只等最慢的 0.4 秒
    assert slow_open == ["third", "second", "first"]  # 确实是后面的先连上
    # 但工具、状态、错误都按配置顺序登记：欢迎页与工具列表的顺序稳定
    assert [name for name, _, _ in manager.status] == ["first", "second", "third"]
    assert [t.name.split("__")[1] for t in manager.tools] == ["first", "second"]
    assert manager.status[2][1] is False and "third 起不来" in manager.errors[0]
    assert list(manager.clients) == ["first", "second"]  # 连不上的不进连接表


def test_servers_close_at_the_same_time(tmp_path: Path, slow_open: list[str]) -> None:
    manager = MCPManager(_configs(), tmp_path)
    clients = list(manager.clients.values())
    start = time.perf_counter()
    manager.close()
    assert time.perf_counter() - start < DELAY * 1.8  # 两个各 0.4 秒，排队要 0.8 秒
    assert all(c.closed for c in clients) and manager.clients == {}
    manager.close()  # 重复关不出错（界面和命令行入口退出时都会关）


# ---------- 后台连接（交互界面：欢迎页不等 MCP，用户提交输入时再接入） ----------


def test_background_returns_at_once_then_registers_in_order(tmp_path: Path, slow_open: list[str]) -> None:
    start = time.perf_counter()
    manager = MCPManager(_configs(), tmp_path, background=True)
    assert time.perf_counter() - start < DELAY / 2  # 不等任何一个服务
    assert not manager.ready and manager.pending() == ["first", "second", "third"]
    assert [(name, ok, detail) for name, ok, detail in manager.status] == [
        ("first", False, CONNECTING), ("second", False, CONNECTING), ("third", False, CONNECTING)
    ]  # fmt: skip
    assert manager.wait(5)
    assert manager.ready and manager.pending() == []
    assert [name for name, _, _ in manager.status] == ["first", "second", "third"]  # 占位原地替换，顺序不变
    assert [t.name.split("__")[1] for t in manager.tools] == ["first", "second"]
    manager.close()


def test_close_while_connecting_waits_then_closes(tmp_path: Path, slow_open: list[str]) -> None:
    """启动后马上退出：要等后台连完再关，否则刚启动的服务进程没人关，成了孤儿进程。"""
    manager = MCPManager(_configs(), tmp_path, background=True)
    manager.close()
    assert manager.ready and manager.clients == {}
    assert len(slow_open) == 3  # 三个都连完了才关


def _repl(tmp_path: Path, manager: MCPManager) -> tuple[Any, Console]:
    from fake_model import FakeModel
    from heng_agent.agent import Agent
    from heng_agent.core.config import Settings
    from heng_agent.ui.repl import Repl

    work = tmp_path / "w"
    work.mkdir()
    console = Console(record=True, width=140, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([])), console=console,
                mcp_status=manager.status)  # fmt: skip
    repl.mcp_managers.insert(0, manager)
    return repl, console


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch, slow_open: list[str]) -> threading.Event:
    """闸门：放行之前假服务一个都连不上（建界面要花不少时间，不拦住的话欢迎页之前就连完了，测不到「连接中」）。"""
    released = threading.Event()
    slow = MCPManager._open

    def gated(self: MCPManager, config: ServerConfig) -> Any:
        released.wait(10)
        return slow(self, config)

    monkeypatch.setattr(MCPManager, "_open", gated)
    return released


def test_repl_banner_then_sync_on_first_input(tmp_path: Path, gate: threading.Event) -> None:
    manager = MCPManager(_configs(), tmp_path, background=True)
    repl, console = _repl(tmp_path, manager)
    repl.banner()
    gate.set()
    repl._sync_mcp()  # 用户提交第一句：等后台连完，再把工具接进智能体
    repl._sync_mcp()  # 之后再提交：已经接过，什么都不做
    text = console.export_text()
    banner, after = text.split("个服务正在后台连接", 1)
    assert "后台连接中（3 个）" in banner and "连接失败" not in banner  # 连接中不能当成失败标红
    assert after.count("MCP：2 个服务已连接 · 2 个工具") == 1  # 连完打印一次汇总
    assert 'MCP 服务 "third" 连接失败：third 起不来' in after
    assert {"mcp__first__first_tool", "mcp__second__second_tool"} <= set(repl.agent.tools)
    manager.close()


def test_repl_ctrl_c_skips_waiting(tmp_path: Path, gate: threading.Event) -> None:
    manager = MCPManager(_configs(), tmp_path, background=True)
    repl, console = _repl(tmp_path, manager)

    def interrupted(timeout: float | None = None) -> bool:
        raise KeyboardInterrupt  # 等待时用户按了 Ctrl+C

    manager.wait = interrupted  # type: ignore[method-assign]
    repl._sync_mcp()
    assert "已跳过等待" in console.export_text()
    assert not any(name.startswith("mcp__") for name in repl.agent.tools)  # 这一句不带 MCP 工具
    del manager.wait  # 只恢复 wait（monkeypatch.undo 会把假服务、闸门也一起撤掉）
    gate.set()
    repl._sync_mcp()  # 下次输入时接上
    assert "mcp__first__first_tool" in repl.agent.tools
    manager.close()


def test_repl_syncs_even_if_connected_before_banner(tmp_path: Path, slow_open: list[str]) -> None:
    """服务连得比建界面还快：界面拿到的已是连好的状态。以前按「状态里有没有连接中」判断，工具永远接不进智能体。"""
    manager = MCPManager(_configs(), tmp_path, background=True)
    assert manager.wait(5)
    repl, console = _repl(tmp_path, manager)
    repl.banner()  # 欢迎页直接显示连接结果
    repl._sync_mcp()
    assert "mcp__first__first_tool" in repl.agent.tools
    assert console.export_text().count("MCP：2 个服务已连接") == 1  # 欢迎页已经显示过，不重复打印
    manager.close()

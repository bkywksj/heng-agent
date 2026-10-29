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
        self.span = (0.0, 0.0)  # 关闭从什么时候开始、到什么时候结束

    def close(self) -> None:
        begin = time.perf_counter()
        time.sleep(DELAY)
        self.closed = True
        self.span = (begin, time.perf_counter())


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
    manager.close()
    # 看两段关闭的时间有没有交叠：排队关的话后一个要等前一个关完才开始。以前比总用时（< 0.72 秒，排队 0.8 秒），
    # GitHub 的 macOS 机器慢，0.724 秒挂过一次（0.5.0 发版时）
    assert max(c.span[0] for c in clients) < min(c.span[1] for c in clients)
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
    assert "正在后台连接" not in text  # 状态栏已经显示连接进度：开头不再单独写一行
    banner, after = text.split("MCP 服务 ", 1)  # 连完后才打印每个服务一行
    assert "后台连接中（3 个）" in banner and "连接失败" not in banner  # 连接中不能当成失败标红
    after = "MCP 服务 " + after
    assert after.count("MCP：2 个服务已连接 · 2 个工具") == 1  # 连完打印一次汇总
    assert 'MCP 服务 "third" 连接失败：third 起不来' in after and 'MCP 服务 "first" 已连接' in after
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


def test_toolbar_shows_live_mcp_status_and_wakes_input(tmp_path: Path, gate: threading.Event) -> None:
    """以前后台连好了也看不出来：欢迎页写着「后台连接中」，要发出下一句才打印结果（用户反馈「看不到 MCP 的连接情况」）。
    现在状态栏按实时状态显示，连完时叫醒正开着的输入框重画，不用等用户提交。"""
    from heng_agent.ui import mcp_view

    manager = MCPManager(_configs(), tmp_path, background=True)
    repl, _ = _repl(tmp_path, manager)
    woke = threading.Event()
    manager.on_done(woke.set)
    style, text = mcp_view.toolbar_part(repl) or ("", "")
    assert style == "class:bar.mcp-wait" and text.endswith(" MCP 0") and text.strip()[0] in mcp_view.SPINNER
    gate.set()
    assert woke.wait(5)  # 连完（不论成败）回调一次，不用等用户提交输入
    assert mcp_view.toolbar_part(repl) == ("class:bar.mcp-fail", "  ✗ MCP 2")  # 还没 _sync_mcp 也是实时的
    called: list[int] = []
    manager.on_done(lambda: called.append(1))  # 已经连完再登记：立刻调
    assert called == [1]
    manager.close()


def test_toolbar_mcp_part_absent_without_servers(tmp_path: Path) -> None:
    from heng_agent.ui import mcp_view
    from heng_agent.ui.prompt import toolbar

    manager = MCPManager([], tmp_path, background=True)
    repl, _ = _repl(tmp_path, manager)
    assert mcp_view.toolbar_part(repl) is None
    bar = "".join(t for _, t in toolbar("ask", "m", "w", 0, 1, mcp=("class:bar.mcp", "  MCP 6")))
    assert "MCP 6" in bar


def test_input_box_refresh_is_noop_when_closed(tmp_path: Path) -> None:
    from heng_agent.ui.inputbox import InputBox

    box = InputBox.__new__(InputBox)
    box.app = None
    box.refresh()  # 输入框没开着（模型正在回答）时连完：什么都不做，不报错


def test_toolbar_progress_counts_each_server_as_it_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """以前结果要全部连完才登记，状态栏从 0/3 直接跳到全部（用户反馈「没有数量变化」）；现在每连完一个就加一。"""
    from heng_agent.ui import mcp_view

    gates = {name: threading.Event() for name in ("first", "second", "third")}

    def fake_open(self: MCPManager, config: ServerConfig) -> Any:
        gates[config.name].wait(10)
        return FakeClient(config), [{"name": "t"}]

    monkeypatch.setattr(MCPManager, "_open", fake_open)
    manager = MCPManager(_configs(), tmp_path, background=True)
    repl, _ = _repl(tmp_path, manager)

    def shown() -> str:
        return (mcp_view.toolbar_part(repl) or ("", ""))[1]

    assert shown().endswith(" MCP 0")
    gates["second"].set()
    deadline = time.monotonic() + 5
    while manager.opened < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert shown().endswith(" MCP 1") and shown().strip()[0] in mcp_view.SPINNER
    # 连接中、连好、连不上三种写法一样宽：状态栏不因为 MCP 状态变化而抖（用户要求只放图标、MCP 与数量）
    assert len(shown()) == len("  ✓ MCP 1") == len("  ✗ MCP 1")
    gates["first"].set()
    gates["third"].set()
    assert manager.wait(5)
    assert shown() == "  ✓ MCP 3"
    manager.close()


def test_ready_servers_are_reported_before_input_without_waiting(tmp_path: Path, gate: threading.Event) -> None:
    """欢迎页后有确认问题挡着时，答完服务已经连好：输入框出现前就打出结果（带用时）；还没连好就不等。"""
    manager = MCPManager(_configs(), tmp_path, background=True)
    repl, console = _repl(tmp_path, manager)
    repl.banner()
    repl._sync_mcp(wait=False)  # 还在连：不等、不打印、工具也先不接
    assert "mcp__first__first_tool" not in repl.agent.tools
    gate.set()
    assert manager.wait(5)
    repl._sync_mcp(wait=False)
    text = console.export_text()
    assert "MCP：2 个服务已连接 · 2 个工具，1 个连不上，用时" in text and "mcp__first__first_tool" in repl.agent.tools
    manager.close()


def test_connected_while_input_open_prints_above_or_waits(tmp_path: Path, gate: threading.Event) -> None:
    """连完时输入框开着：交给输入框打印到它上方（以前只更新状态栏，用户回答得快时看不到连了哪些服务）；
    输入框没开着：不打印，等下次显示输入框前再接。"""
    manager = MCPManager(_configs(), tmp_path, background=True)
    repl, console = _repl(tmp_path, manager)  # 建界面时还在连（真实启动也是这样）
    gate.set()
    assert manager.wait(5)

    class Box:
        def __init__(self, running: bool) -> None:
            self.running, self.funcs, self.refreshed = running, [], 0

        def print_above(self, func: Any) -> bool:
            if self.running:
                self.funcs.append(func)
            return self.running

        def refresh(self) -> None:
            self.refreshed += 1

    closed = Box(running=False)
    repl._box = closed  # type: ignore[assignment]
    repl._mcp_connected()
    assert closed.refreshed == 1 and "mcp__first__first_tool" not in repl.agent.tools
    open_box = Box(running=True)
    repl._box = open_box  # type: ignore[assignment]
    repl._mcp_connected()
    open_box.funcs[0]()  # 输入框在界面线程里调它
    assert 'MCP 服务 "first" 已连接' in console.export_text() and "mcp__first__first_tool" in repl.agent.tools
    manager.close()


def test_print_above_needs_a_running_input_box() -> None:
    from heng_agent.ui.inputbox import InputBox

    box = InputBox.__new__(InputBox)
    box.app = None
    assert box.print_above(lambda: None) is False


def test_builtin_commands_do_not_wait_for_mcp(tmp_path: Path, gate: threading.Event) -> None:
    """内置命令、# 记忆在本地处理，用不上 MCP：不等它连好（以前 /effort 也要等十几秒）。发给模型的话照样等。"""
    manager = MCPManager(_configs(), tmp_path, background=True)
    repl, console = _repl(tmp_path, manager)
    lines = iter(["/version", "/exit"])
    repl.read = lambda: next(lines)  # type: ignore[method-assign]
    start = time.monotonic()
    assert repl.loop() == 0
    assert time.monotonic() - start < 3  # 闸门没放：要是等了，会一直等到闸门超时（10 秒）
    assert "等待 MCP 服务连接" not in console.export_text()
    gate.set()
    manager.close()

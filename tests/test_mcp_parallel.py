"""MCP 服务同时连、同时关（以前一个接一个：6 个服务启动实测 10 秒、退出 5 秒），且顺序不随谁先连上而变。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from heng_agent.core.errors import AgentError
from heng_agent.mcp import MCPManager, ServerConfig

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

"""核心循环不在主线程跑时（--acp）的中断：靠取消标志，不靠给主线程发 KeyboardInterrupt。"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import ToolFinished, ToolStarted


def _agent(tmp_path: Path, script: list[Any]) -> Agent:
    return Agent(Settings(workdir=tmp_path, home=tmp_path / "h", permission_mode="auto"), FakeModel(script))


def _run_in_thread(agent: Agent, on_event: Any) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            outcome["done"] = agent.run("干活", on_event, lambda tool, args: "once")
        except KeyboardInterrupt:
            outcome["interrupted"] = True

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, outcome


def test_cancel_long_command_from_another_thread(tmp_path: Path) -> None:
    sleeper = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    agent = _agent(tmp_path, [[("tool", "bash", {"command": sleeper})], ["不该走到这里"]])
    started = threading.Event()
    thread, outcome = _run_in_thread(agent, lambda e: started.set() if isinstance(e, ToolStarted) else None)
    assert started.wait(10)
    time.sleep(0.5)  # 命令确实跑起来了
    begin = time.monotonic()
    agent.cancel()  # 从主线程取消：干活的线程不是主线程，不能靠给主线程发中断
    thread.join(10)
    assert not thread.is_alive() and outcome.get("interrupted")
    assert time.monotonic() - begin < 5  # 很快停下，不是等 30 秒的命令自己结束
    assert len(agent.client.calls) == 1  # type: ignore[attr-defined]  # 没有再请求模型


def test_cancel_skips_remaining_tool_calls(tmp_path: Path) -> None:
    quick = f'"{sys.executable}" -c "print(1)"'
    agent = _agent(tmp_path, [[("tool", "bash", {"command": quick}), ("tool", "bash", {"command": quick})]])
    started: list[str] = []

    def on_event(event: Any) -> None:
        if isinstance(event, ToolStarted):
            started.append(event.id)
        if isinstance(event, ToolFinished):
            agent.cancel()  # 第一个工具做完就取消

    thread, outcome = _run_in_thread(agent, on_event)
    thread.join(20)
    assert outcome.get("interrupted") and len(started) == 1  # 第二个工具没有跑


def test_stale_cancel_does_not_block_next_turn(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [["好的"]])
    agent.ctx.cancelled.set()  # 上一问留下的取消
    thread, outcome = _run_in_thread(agent, lambda e: None)
    thread.join(10)
    assert "done" in outcome and agent.messages[-1]["content"][-1]["text"] == "好的"


def test_subagent_shares_cancel_flag(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [])
    sub, _ = agent._spawn_subagent()
    assert sub.ctx.cancelled is agent.ctx.cancelled  # 取消主任务时子智能体一起停


def test_subagent_does_not_swallow_parent_cancel(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [])
    sub, _ = agent._spawn_subagent()
    agent.ctx.cancelled.set()  # 主任务刚被取消
    sub.client = FakeModel([["子任务的回复"]])
    with contextlib.suppress(KeyboardInterrupt):  # 子智能体看到取消、停下：正确
        sub.run("调研一下", lambda e: None, lambda tool, args: "deny")
    assert agent.ctx.cancelled.is_set()  # 子智能体开始时没有把主任务的取消清掉

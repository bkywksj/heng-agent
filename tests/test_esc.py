"""ESC 全套：选择器、输入框里的 ESC、取消模型请求不重试、命令能被及时打断、中断后历史完整、/rewind。"""

from __future__ import annotations

import _thread
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import TextDelta
from heng_agent.model.client import make_client
from heng_agent.model.providers import Profile
from heng_agent.tools.shell import run_command
from heng_agent.ui.inputbox import REWIND, InputBox
from heng_agent.ui.prompt import MODES, STYLE, SlashCompleter
from heng_agent.ui.select import Option, choose

DOWN, ESC, ENTER = "\x1b[B", "\x1b", "\r"
OPTIONS = [Option("once", "这次允许", "y"), Option("always", "一直允许", "a"), Option("deny", "拒绝", "n")]


def _choose(keys: str, later: str = "") -> str | None:
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        if later:
            threading.Timer(0.3, pipe.send_text, [later]).start()
        return choose("允许吗？", OPTIONS, input=pipe, output=DummyOutput())


def test_selector_keys() -> None:
    assert _choose(ENTER) == "once"
    assert _choose(DOWN + DOWN + ENTER) == "deny"  # 方向键移动（VT 序列不会被当成 ESC）
    assert _choose("a") == "always"  # 快捷字母直接选
    assert _choose("", later=ESC) is None  # 单独的 ESC：取消


def _box() -> tuple[InputBox, Any]:
    pipe = create_pipe_input().__enter__()
    box = InputBox(history=InMemoryHistory(), commands=SlashCompleter(lambda: {}), workdir=Path.cwd(),
                   toolbar=lambda: "", get_mode=lambda: "ask", set_mode=lambda m: None, modes=MODES, style=STYLE,
                   input=pipe, output=DummyOutput())  # fmt: skip
    return box, pipe


def test_esc_in_input_box() -> None:
    box, pipe = _box()
    pipe.send_text("写了一半")
    threading.Timer(0.3, pipe.send_text, [ESC]).start()  # ESC 清空
    threading.Timer(0.6, pipe.send_text, ["重写" + ENTER]).start()
    assert box.prompt() == "重写"
    box, pipe = _box()
    threading.Timer(0.2, pipe.send_text, [ESC]).start()
    threading.Timer(0.5, pipe.send_text, [ESC]).start()  # 空框里双击 ESC → 回退
    assert box.prompt() == REWIND
    box, pipe = _box()
    pipe.send_text("改一下" + ENTER)
    assert box.prompt(default="原来的问题 ") == "原来的问题 改一下"  # 预填（/rewind 把那一问放回来）


# ---------- 取消模型请求 ----------


def test_cancel_turns_read_error_into_interrupt_without_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadError("连接被关闭")

    profile = Profile(protocol="anthropic", base_url="http://x", model="m")
    client = make_client(profile, 100, transport=httpx.MockTransport(handler))
    client.sleep = lambda s: None
    with pytest.raises(Exception, match="中断") as normal:  # 没取消：网络错误会被重试 4 次后报错
        list(client.stream("", [{"role": "user", "content": "hi"}], []))
    assert len(calls) == 5 and "KeyboardInterrupt" not in normal.typename
    calls.clear()

    original = client._post_stream_inner

    def cancelled_midway(*args: Any) -> Iterator[str]:
        client._cancelled.set()  # 模拟：读到一半用户按了 ESC，连接随即被关
        yield from original(*args)

    client._post_stream_inner = cancelled_midway  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        list(client.stream("", [{"role": "user", "content": "hi"}], []))
    assert len(calls) == 1  # 用户取消的不重试


def test_agent_cancel_picks_method_by_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([]))
    cancelled, interrupted = [], []
    agent.client.cancel = lambda: cancelled.append(1)  # type: ignore[attr-defined]
    monkeypatch.setattr(_thread, "interrupt_main", lambda: interrupted.append(1))
    agent.cancel()
    assert not cancelled and not interrupted  # 空闲时什么都不做
    agent._phase = "model"
    agent.cancel()
    agent._phase = "tool"
    agent.cancel()
    assert cancelled == [1] and interrupted == [1]  # 等模型关连接；跑工具才打断主线程（两者不能同时用）


class InterruptedModel:
    """吐出半段文字后被打断（模拟用户按 ESC）。"""

    def stream(self, system: str, messages: list[Any], tools: list[Any]) -> Iterator[Any]:
        yield TextDelta("我先看看")
        raise KeyboardInterrupt


def test_interrupt_midway_keeps_history_alternating(tmp_path: Path) -> None:
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), InterruptedModel())
    with pytest.raises(KeyboardInterrupt):
        agent.run("帮我看看", lambda e: None, lambda t, a: "deny")
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]  # 半段回复记下了，下一问不会两条用户消息相连
    assert (
        "被用户中断" in agent.messages[-1]["content"][0]["text"]
        and "我先看看" in agent.messages[-1]["content"][0]["text"]
    )
    agent.client = FakeModel([["好的"]])
    agent.run("继续", lambda e: None, lambda t, a: "deny")
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user", "assistant"]


def test_command_is_interrupted_promptly(tmp_path: Path) -> None:
    """Windows 上曾要等命令自己跑完才停（proc.wait 的超时被算成负数，一直等）。现在应在 1 秒内停下。"""
    import sys

    threading.Timer(0.5, _thread.interrupt_main).start()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_command([sys.executable, "-c", "import time; time.sleep(20)"], tmp_path, 60, None)
    assert time.monotonic() - started < 3


# ---------- /rewind ----------


def test_rewind_restores_files_and_history(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    (work / "a.py").write_text("v0", encoding="utf-8")
    model = FakeModel([
        [("tool", "write_file", {"path": "a.py", "content": "v1"})], ["改成 v1"],
        [("tool", "write_file", {"path": "a.py", "content": "v2"}),
         ("tool", "write_file", {"path": "b.py", "content": "新"})],
        ["改成 v2 并新建 b"],
        ["只是聊天"],
    ])  # fmt: skip
    agent = Agent(Settings(workdir=work, home=tmp_path / "h", permission_mode="auto"), model)
    for q in ("第一问：改 v1", "第二问：改 v2", "第三问：聊聊"):
        agent.run(q, lambda e: None, lambda t, a: "once")
    assert agent.turn_titles() == ["第一问：改 v1", "第二问：改 v2", "第三问：聊聊"]
    restored = agent.rewind(0)  # 回到第一问之前
    assert (work / "a.py").read_text(encoding="utf-8") == "v0" and not (work / "b.py").exists()
    assert set(restored) == {"a.py", "b.py"} and agent.messages == [] and agent.turns == []


def test_rewind_command_prefills_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.console import Console

    from heng_agent.ui.repl import Repl

    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([["一"], ["二"]]))
    agent.run("第一问", lambda e: None, lambda t, a: "deny")
    agent.run("第二问\n第二行", lambda e: None, lambda t, a: "deny")
    repl = Repl(agent, console=Console(record=True, width=100, force_terminal=False))
    monkeypatch.setattr(repl, "_ask", lambda prompt="": "2")  # 选择器用不了时退回打编号
    repl.handle_command("/rewind")
    assert len(agent.turns) == 1 and repl._prefill == "第二问\n第二行"  # 那一问放回输入框，等改了重发

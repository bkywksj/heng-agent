"""子智能体面板、后台子智能体（task 的 run_in_background）、查看子智能体做了什么。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import TextDelta, ToolCall, TurnEnd
from heng_agent.tools.subagent import SUBAGENT_PROMPT
from heng_agent.ui.repl import Repl
from heng_agent.ui.repl_subagents import NOTICE_PREFIX


class Routed:
    """主智能体与子智能体共用一个客户端：按「是不是子智能体的请求」分开走各自的脚本（后台跑时先后不定）。
    子智能体可以先卡住（gate），测试决定它什么时候做完。"""

    def __init__(self, main: list[list[Any]], sub: list[list[Any]], gate: threading.Event | None = None) -> None:
        self.main, self.sub = FakeModel(main), FakeModel(sub)
        self.gate = gate
        self.lock = threading.Lock()

    def stream(self, system: str, messages: list[Any], tools: list[Any]) -> Any:
        first = messages[0]["content"]
        text = first if isinstance(first, str) else "".join(b.get("text", "") for b in first if isinstance(b, dict))
        if text.startswith(SUBAGENT_PROMPT.split("\n", 1)[0]):
            if self.gate is not None:
                self.gate.wait(5)
            with self.lock:
                events = list(self.sub.stream(system, messages, tools))
            yield from events
            return
        with self.lock:
            events = list(self.main.stream(system, messages, tools))
        yield from events


def _wait(cond: Any, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)


def _agent(tmp_path: Path, client: Any) -> Agent:
    return Agent(Settings(workdir=tmp_path, home=tmp_path / "h", permission_mode="auto"), client)


TASK = {"description": "找入口", "prompt": "找程序入口", "run_in_background": True}


def test_background_returns_at_once_and_notifies_when_done(tmp_path: Path) -> None:
    gate = threading.Event()
    client = Routed([[("tool", "task", TASK)], ["主智能体先接着干别的"]], [["入口在 cli.py:main"]], gate)
    agent = _agent(tmp_path, client)
    done: list[Any] = []
    agent.background_notify = done.append
    agent.run("看看项目", lambda e: None, lambda t, a: "allow")  # 子智能体还卡着：主智能体这一问照样跑完
    result = agent.messages[2]["content"][0]["content"]
    assert "已派到后台" in result and done == []
    run = agent.subruns.all()[0]
    assert run.running and run.background
    gate.set()
    _wait(lambda: done)
    assert done == [run] and not run.running and run.result == "入口在 cli.py:main"


def test_background_needs_a_listener_otherwise_runs_in_foreground(tmp_path: Path) -> None:
    """-p 非交互模式、测试里没人接通知：照旧在前台跑完，结论当场交回。"""
    client = Routed([[("tool", "task", TASK)], ["好"]], [["入口在 cli.py:main"]])
    agent = _agent(tmp_path, client)
    agent.run("看看项目", lambda e: None, lambda t, a: "allow")
    assert "入口在 cli.py:main" in agent.messages[2]["content"][0]["content"]
    assert not agent.subruns.all()[0].background


def test_background_subagent_survives_main_cancel(tmp_path: Path) -> None:
    """主对话按 ESC：后台子智能体不跟着停（它有自己的取消标志）。"""
    gate = threading.Event()
    client = Routed([[("tool", "task", TASK)], ["好"]], [["结论"]], gate)
    agent = _agent(tmp_path, client)
    done: list[Any] = []
    agent.background_notify = done.append
    agent.run("看看项目", lambda e: None, lambda t, a: "allow")
    agent.cancel()
    gate.set()
    _wait(lambda: done)
    assert done[0].result == "结论" and not done[0].error


def test_background_failure_is_reported(tmp_path: Path) -> None:
    class Broken(Routed):
        def stream(self, system: str, messages: list[Any], tools: list[Any]) -> Any:
            if str(messages[0]["content"]).find("调研子智能体") >= 0:
                raise RuntimeError("服务挂了")
            yield from super().stream(system, messages, tools)

    agent = _agent(tmp_path, Broken([[("tool", "task", TASK)], ["好"]], []))
    done: list[Any] = []
    agent.background_notify = done.append
    agent.run("看看项目", lambda e: None, lambda t, a: "allow")
    _wait(lambda: done)
    assert done[0].error and "服务挂了" in done[0].result and not done[0].running


def _repl(tmp_path: Path, client: Any) -> tuple[Repl, Console]:
    console = Console(record=True, width=100, force_terminal=False)
    return Repl(_agent(tmp_path, client), console=console), console


def test_finished_notice_is_injected_during_a_turn(tmp_path: Path) -> None:
    """回答中做完：这批工具跑完时插进这一问，显示成一行提示（不是「你说的话」）。"""

    class Model:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def stream(self, system: str, messages: list[Any], tools: list[Any]) -> Any:
            self.calls.append(messages)
            if len(self.calls) == 1:
                yield ToolCall(id="t1", name="glob", input={"pattern": "*"})
                yield TurnEnd("tool_use", {"input_tokens": 1, "output_tokens": 1})
            else:
                yield TextDelta("收到结论")
                yield TurnEnd("end_turn", {"input_tokens": 1, "output_tokens": 1})

    repl, console = _repl(tmp_path, Model())
    repl._notices.append(f"{NOTICE_PREFIX}#1「找入口」做完了（用时 3s · 2 轮）。结论：\n\n入口在 cli.py")
    repl._queue_lock = threading.Lock()  # 平时由 _run_turn_live 建好
    repl.agent.interjections = repl._take_interjections
    repl.agent.run("看看", repl.on_event, lambda t, a: "allow")
    sent = repl.agent.messages[2]["content"]
    assert any(b.get("type") == "text" and b["text"].startswith(NOTICE_PREFIX) for b in sent)
    text = console.export_text()
    assert "◆ 后台子智能体 #1「找入口」做完了" in text and "▌ 【后台" not in text


def test_idle_notice_starts_a_turn(tmp_path: Path, monkeypatch: Any) -> None:
    """空闲时做完：主循环自动发起一问把结论交给主智能体，显示成一行提示。"""
    client = FakeModel([["好的，我看到结论了"]])
    repl, console = _repl(tmp_path, client)
    repl._notices.append(f"{NOTICE_PREFIX}#2「查配置」做完了（用时 5s · 1 轮）。结论：\n\n配置在 a.json")
    reads = iter([EOFError])

    def read() -> str:
        raise next(reads)

    monkeypatch.setattr(repl, "read", read)
    monkeypatch.setattr(repl, "banner", lambda: None)
    repl.loop()
    assert client.calls and "配置在 a.json" in str(client.calls[0][-1])
    text = console.export_text()
    assert "◆ 后台子智能体 #2「查配置」做完了" in text and "好的，我看到结论了" in text


def test_notify_signal_from_idle_box_goes_through_read(tmp_path: Path, monkeypatch: Any) -> None:
    """输入框被叫醒时 prompt 返回 TurnSignal：read() 要原样交回，不能当字符串 .strip()——以前程序当场退出
    （真实终端里实测：后台子智能体一做完窗口就回到了 PowerShell）。"""
    from heng_agent.ui.inputbox import TurnSignal

    repl, _ = _repl(tmp_path, FakeModel([]))
    repl._plain_input = False
    monkeypatch.setattr(repl, "_input", lambda message: TurnSignal("notify", "半句草稿"))
    assert repl.read() == TurnSignal("notify", "半句草稿")


def test_panel_rows_and_view(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path, FakeModel([]))
    run = repl.agent.subruns.add("查各家模型上下文长度", "通用调研员", background=True)
    run.add_tool("grep pattern=context")
    run.add_text("找到了 3 处")
    rows = repl._agent_rows()
    assert rows[0][0] == run.id and "general  查各家模型上下文长度" in rows[0][1] and "后台" in rows[0][1]
    repl.view_subagent(run.id)
    text = console.export_text()
    assert "子智能体 #1：查各家模型上下文长度" in text and "● grep pattern=context" in text and "找到了 3 处" in text
    run.finished = time.monotonic()
    assert repl._agent_rows() == []  # 做完了就从面板上消失


def test_down_selects_agent_and_enter_views(tmp_path: Path) -> None:
    """输入框最后一行按 ↓ 进入子智能体面板，Enter 查看选中的那个；输入框里的字不受影响。"""
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from heng_agent.ui.inputbox import InputBox
    from heng_agent.ui.prompt import MODES, STYLE, SlashCompleter

    viewed: list[int] = []
    with create_pipe_input() as pipe:
        box = InputBox(history=InMemoryHistory(), commands=SlashCompleter(lambda: {}), workdir=tmp_path,
                       toolbar=lambda: "", get_mode=lambda: "ask", set_mode=lambda m: None, modes=MODES, style=STYLE,
                       input=pipe, output=DummyOutput(), agents=lambda: [(7, "a"), (9, "b")],
                       on_view_agent=viewed.append)  # fmt: skip
        pipe.send_text("草稿")
        pipe.send_text("\x1b[B\x1b[B\r")  # ↓ 进入面板、↓ 选第二个、Enter 查看
        pipe.send_text("\r")  # 退出面板后 Enter 照常发送
        assert box.prompt() == "草稿"
    assert viewed == [9]

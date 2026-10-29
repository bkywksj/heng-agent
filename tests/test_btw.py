"""/btw 旁支问题：看得到之前的对话、不带工具、不进对话历史、回答中也能问（不打断正在进行的那一问）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import TextDelta, TurnEnd
from heng_agent.ui import btw
from heng_agent.ui.repl import Repl


class Recorder:
    """假的旁支问题客户端：记下收到的消息与工具，回一句固定的话。"""

    def __init__(self, answer: str = "旁支的回答") -> None:
        self.answer, self.calls = answer, []

    def stream(self, system: str, messages: list[Any], tools: list[Any]) -> Any:
        self.calls.append((messages, tools))
        yield TextDelta(self.answer)
        yield TurnEnd("end_turn", {"input_tokens": 1, "output_tokens": 1})


def test_snapshot_drops_unanswered_tool_call() -> None:
    """回答进行到一半时最后一条可能是还没结果的 tool_use：原样发出去服务会拒收，去掉它。"""
    call = {"type": "tool_use", "id": "t1", "name": "glob", "input": {}}
    history = [{"role": "user", "content": [{"type": "text", "text": "看看代码"}]},
               {"role": "assistant", "content": [call]}]  # fmt: skip
    assert btw.snapshot(history) == history[:1]
    assert history[-1]["content"][0]["type"] == "tool_use"  # 原对话不动


def test_build_carries_previous_side_questions() -> None:
    msgs = btw.build([], [("上一个问题", "上一个回答")], "这一个")
    texts = [m["content"][0]["text"] for m in msgs]
    assert texts[0].endswith("上一个问题") and texts[1] == "上一个回答" and texts[2].endswith("这一个")
    assert "不要调用任何工具" in texts[2]


def _repl(tmp_path: Path) -> tuple[Repl, Console]:
    console = Console(record=True, width=100, force_terminal=False)
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([]))
    agent.messages = [{"role": "user", "content": [{"type": "text", "text": "我们在改购物车"}]},
                      {"role": "assistant", "content": [{"type": "text", "text": "好的"}]}]  # fmt: skip
    return Repl(agent, console=console), console


def _wait(cond: Any) -> None:
    deadline = time.monotonic() + 5
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)


def test_btw_sees_conversation_uses_no_tools_and_stays_out_of_history(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    client = Recorder()
    repl._btw_client = lambda: client  # type: ignore[method-assign]
    before = list(repl.agent.messages)
    repl.handle_command("/btw 刚才说的是哪个文件")
    _wait(lambda: repl._btw_log)
    messages, tools = client.calls[0]
    # 带着主对话的工具声明（只为吃缓存，不执行）：以前传空列表，前缀对不上，整段对话每次按全价算
    assert tools == repl.agent.tool_specs() and tools
    assert messages[0]["content"][0]["text"] == "我们在改购物车"  # 看得到之前的对话
    assert repl.agent.messages == before  # 不进对话历史
    assert repl._btw_log == [("刚才说的是哪个文件", "旁支的回答")]
    shown = console.export_text()
    assert "旁支问题：刚才说的是哪个文件" in shown and "旁支的回答" in shown


def test_btw_errors_are_reported(tmp_path: Path) -> None:
    from heng_agent.core.errors import ModelError

    repl, console = _repl(tmp_path)

    class Broken:
        def stream(self, *args: Any) -> Any:
            raise ModelError("模型服务返回 500")
            yield

    repl._btw_client = lambda: Broken()  # type: ignore[method-assign]
    repl.handle_command("/btw 问一句")
    _wait(lambda: "没问成" in console.export_text(clear=False))
    assert "旁支问题没问成：模型服务返回 500" in console.export_text()


def test_btw_needs_a_question(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    repl.handle_command("/btw")
    assert "用法：/btw 问题" in console.export_text()


def test_btw_during_turn_runs_at_once_without_interrupting(tmp_path: Path) -> None:
    """回答中输入 /btw：马上发出（不排队到这一问结束），正在进行的那一问照常跑完。"""
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from heng_agent.ui.inputbox import InputBox
    from heng_agent.ui.prompt import MODES, STYLE, SlashCompleter
    from test_live_turn import SlowModel

    with create_pipe_input() as pipe:
        console = Console(record=True, width=100, force_terminal=False)
        repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), SlowModel([["主回答"]], delay=1.2)),
                    console=console)  # fmt: skip
        repl._box = InputBox(history=InMemoryHistory(), commands=SlashCompleter(lambda: {}), workdir=tmp_path,
                             toolbar=lambda: "", get_mode=lambda: "ask", set_mode=lambda m: None, modes=MODES,
                             style=STYLE, input=pipe, output=DummyOutput())  # fmt: skip
        client = Recorder("旁支答好了")
        repl._btw_client = lambda: client  # type: ignore[method-assign]
        threading.Timer(0.3, pipe.send_text, ["/btw 顺便问一下\r"]).start()
        repl._run_turn_live("主问题", None)
    assert repl._queued == [] and repl._btw_log == [("顺便问一下", "旁支答好了")]
    text = console.export_text()
    assert "旁支答好了" in text and "主回答" in text  # 两个都在，主回答没被打断
    assert all("顺便问一下" not in str(m) for m in repl.agent.messages)  # 旁支问题不进对话历史


def test_btw_tool_call_is_not_run() -> None:
    """模型还是去调了工具：不执行，回答后面注明一句。"""
    from heng_agent.core.types import ToolCall

    class Calls(Recorder):
        def stream(self, system: str, messages: list[Any], tools: list[Any]) -> Any:
            yield TextDelta("我先查一下")
            yield ToolCall("t1", "glob", {"pattern": "*.py"})
            yield TurnEnd("tool_use", {})

    assert btw.ask(Calls(), "", [], [], "哪个文件") == f"我先查一下\n\n{btw.TOOL_NOTE}"

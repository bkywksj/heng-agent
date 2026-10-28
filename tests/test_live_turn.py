"""回答过程中输入框一直在（ui/repl_turn.py）：智能体在后台线程跑，主线程显示输入框。

用管道模拟按键、DummyOutput 代替终端：一问能自己跑完、回答中敲下的话排进队列、确认在主线程里问、ESC 打断。
这些只是逻辑；真实控制台里的显示（输入框上方打印、计时转圈）还要实测（见 test-development 技能）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.ui.inputbox import InputBox
from heng_agent.ui.prompt import MODES, STYLE, SlashCompleter
from heng_agent.ui.repl import Repl

ESC = "\x1b"


class SlowModel(FakeModel):
    """每一轮先等一会儿再回答：好在「回答中」发按键。"""

    def __init__(self, script: list[Any], delay: float) -> None:
        super().__init__(script)
        self.delay = delay

    def stream(self, system: str, messages: list[Any], tools: list[Any]) -> Iterator[Any]:
        deadline = time.monotonic() + self.delay
        while time.monotonic() < deadline:  # 分段等：被取消时（ESC）尽快退出
            if getattr(self, "_cancelled", False):
                raise KeyboardInterrupt
            time.sleep(0.02)
        yield from super().stream(system, messages, tools)

    def cancel(self) -> None:
        self._cancelled = True


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Any]:
    with create_pipe_input() as pipe:  # 上下文对象留在这里：函数返回就被回收、管道随之关掉（CLAUDE.md 记过）

        def make(model: Any) -> tuple[Repl, Console]:
            console = Console(record=True, width=100, force_terminal=False)
            work = tmp_path / "w"
            work.mkdir(exist_ok=True)
            repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "h", permission_mode="accept-edits"), model),
                        console=console)  # fmt: skip
            repl._box = InputBox(history=InMemoryHistory(), commands=SlashCompleter(lambda: {}), workdir=work,
                                 toolbar=lambda: "状态栏", get_mode=lambda: "ask", set_mode=lambda m: None,
                                 modes=MODES, style=STYLE, input=pipe, output=DummyOutput())  # fmt: skip
            return repl, console

        yield make, pipe


def _run(repl: Repl, text: str) -> None:
    """跑一问，最多 10 秒：卡住就让测试失败，别一直挂着。"""
    error: list[BaseException] = []
    done = threading.Event()

    def guard() -> None:
        if not done.wait(10):
            repl.agent.cancel()
            error.append(AssertionError("一问 10 秒还没结束"))

    threading.Thread(target=guard, daemon=True).start()
    try:
        repl._run_turn_live(text, None)
    finally:
        done.set()
    if error:
        raise error[0]


def test_turn_finishes_by_itself_and_output_is_kept(live: Any) -> None:
    make, _ = live
    repl, console = make(FakeModel([["第一段\n\n第二段"]]))
    assert repl._live_turn_ok()
    _run(repl, "你好")
    text = console.export_text()
    assert "第一段" in text and "第二段" in text and "轮 · 输入" in text  # 回复与结尾那行都经真正的控制台打出来了
    assert repl.console is console and repl._box is not None and repl._box.turn is None  # 收尾：恢复原样


def test_typing_during_turn_is_queued(live: Any) -> None:
    make, pipe = live
    repl, _ = make(SlowModel([["好"]], delay=1.0))
    threading.Timer(0.3, pipe.send_text, ["下一句\r"]).start()  # 回答中敲一句、回车
    _run(repl, "你好")
    assert repl._queued == ["下一句"]  # 排进队列，这一问结束后由主循环发出


def test_approval_is_asked_on_main_thread(live: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    make, _ = live
    repl, _ = make(FakeModel([[("tool", "bash", {"command": "echo hi"})], ["跑完了"]]))
    asked: list[str] = []

    def fake_ask(tool: Any, args: dict[str, Any]) -> str:
        asked.append(threading.current_thread().name)  # 确认框只能在主线程弹（输入框也在主线程）
        return "once"

    monkeypatch.setattr(repl, "_ask_approval", fake_ask)
    _run(repl, "跑个命令")
    assert asked == ["MainThread"]


def test_escape_interrupts_the_turn(live: Any) -> None:
    make, pipe = live
    repl, _ = make(SlowModel([["不该出现"]], delay=5.0))
    threading.Timer(0.3, pipe.send_text, [ESC]).start()
    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        _run(repl, "你好")
    assert time.monotonic() - start < 4 and repl._esc_pressed  # 很快停下，按原来的方式收尾（「已中断（按了 ESC）」）

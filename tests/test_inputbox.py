"""带边框输入框：用管道模拟按键，验证发送、换行、补全、切换模式、Ctrl+C / Ctrl+D、历史。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from heng_agent.ui.inputbox import InputBox, PathCompleter
from heng_agent.ui.prompt import MODES, STYLE, SlashCompleter

ENTER, CTRL_J, CTRL_C, CTRL_D, SHIFT_TAB = "\r", "\n", "\x03", "\x04", "\x1b[Z"


def _run(
    keys: str,
    tmp_path: Path,
    state: dict | None = None,
    history: InMemoryHistory | None = None,
    later: str = "",
) -> str:
    """later：0.5 秒后再发的按键——历史加载与补全都是异步的，按键一口气发完会抢在它们前面（真人打字没这么快）。"""
    state = state if state is not None else {"mode": "ask"}
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        timer = threading.Timer(0.5, pipe.send_text, [later]) if later else None
        if timer:
            timer.start()
        box = InputBox(
            history=history or InMemoryHistory(),
            commands=SlashCompleter(lambda: {}),
            workdir=tmp_path,
            toolbar=lambda: "状态栏",
            get_mode=lambda: state["mode"],
            set_mode=lambda m: state.update(mode=m),
            modes=MODES,
            style=STYLE,
            input=pipe,
            output=DummyOutput(),
        )
        return box.prompt()


def test_enter_submits_and_ctrl_j_adds_newline(tmp_path: Path) -> None:
    assert _run("你好" + ENTER, tmp_path) == "你好"
    assert _run("第一行" + CTRL_J + "第二行" + ENTER, tmp_path) == "第一行\n第二行"


def test_shift_tab_cycles_mode(tmp_path: Path) -> None:
    state = {"mode": "ask"}
    _run(SHIFT_TAB + SHIFT_TAB + "x" + ENTER, tmp_path, state)
    assert state["mode"] == "plan"  # ask → accept-edits → plan


def test_ctrl_c_clears_then_interrupts_and_ctrl_d_exits(tmp_path: Path) -> None:
    assert _run("写了一半" + CTRL_C + "重写" + ENTER, tmp_path) == "重写"  # 有字时 Ctrl+C 只清空
    with pytest.raises(KeyboardInterrupt):
        _run(CTRL_C + CTRL_C, tmp_path)  # 空框里连按两次 Ctrl+C = 退出
    assert _run(CTRL_C + "还在" + ENTER, tmp_path) == "还在"  # 只按一次：提示「再按一次」，不退出
    assert _run("留着" + "\x0c" + ENTER, tmp_path) == "留着"  # Ctrl+L 清屏，输入框里的字不丢
    with pytest.raises(EOFError):
        _run(CTRL_D, tmp_path)


def test_history_is_recorded(tmp_path: Path) -> None:
    history = InMemoryHistory()
    _run("第一句" + ENTER, tmp_path, history=history)
    assert list(history.get_strings()) == ["第一句"]
    assert _run("", tmp_path, history=history, later="\x1b[A" + ENTER) == "第一句"  # 上箭头取回上一句


def test_path_completion(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("", encoding="utf-8")
    (tmp_path / "setup.py").write_text("", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()

    def names(text: str) -> list[str]:
        comps = PathCompleter(tmp_path).get_completions(Document(text, len(text)), CompleteEvent())
        return [c.text for c in comps]

    assert names("看看 @s") == ["@src/", "@setup.py"]  # 目录在前，带斜杠
    assert names("@src/a") == ["@src/app.py"]
    assert names("@node") == [] and names("没有at") == []  # 跳过依赖目录；不是 @ 开头不补
    assert names("@../") == []  # 不补全工作目录以外


def test_enter_accepts_completion_first(tmp_path: Path) -> None:
    (tmp_path / "cart.py").write_text("", encoding="utf-8")
    # 输入 @ca（补全菜单自动弹出）→ Tab 选中 → Enter 先把补全填进去，再按一次 Enter 才发送
    assert _run("修 @ca", tmp_path, later="\t" + ENTER + ENTER) == "修 @cart.py"


def test_box_is_one_line_and_grows_with_content(tmp_path: Path) -> None:
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document

    box = InputBox(history=InMemoryHistory(), commands=SlashCompleter(lambda: {}), workdir=tmp_path,
                   toolbar=lambda: "状态栏", get_mode=lambda: "ask", set_mode=lambda m: None, modes=MODES,
                   style=STYLE)  # fmt: skip

    def height(text: str) -> int:
        buffer = Buffer(multiline=True)
        buffer.document = Document(text)
        root, _ = box.build_layout(buffer)
        return root.preferred_height(80, 100).preferred

    assert height("") == 4  # 上边框 + 1 行输入 + 下边框 + 状态栏
    assert height("一\n二\n三") == 6  # 随内容长高
    assert height("\n".join(["x"] * 30)) == 13  # 最多 10 行（+ 上下边框 2 行 + 状态栏 1 行），更多的在框内滚动


def test_box_stays_small_when_terminal_has_lots_of_room(tmp_path: Path) -> None:
    """模拟真实运行：非全屏模式下 prompt_toolkit 会把光标下方所有空行（这里 40 行）都交给界面。
    上一版只测了「想要多高」，实际被拉到窗口底部——这里按实际分到的空间画出来，看下边框落在第几行。"""
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition

    box = InputBox(history=InMemoryHistory(), commands=SlashCompleter(lambda: {}), workdir=tmp_path,
                   toolbar=lambda: "状态栏", get_mode=lambda: "ask", set_mode=lambda m: None, modes=MODES,
                   style=STYLE)  # fmt: skip

    def rows_with(text: str, char: str) -> list[int]:
        import asyncio

        buffer = Buffer(multiline=True)
        buffer.document = Document(text)
        root, _ = box.build_layout(buffer)
        screen = Screen()

        async def render() -> None:  # 画输入区时会顺带触发历史的异步加载，需要正在运行的事件循环
            root.write_to_screen(screen, MouseHandlers(), WritePosition(0, 0, 80, 40), "", True, None)

        asyncio.run(render())
        return [y for y in range(40) if any(c.char == char for c in screen.data_buffer[y].values())]

    assert rows_with("", "╰") == [2] and rows_with("", "│") == [1]  # 空框：上边框、1 行输入、下边框
    assert rows_with("一\n二\n三", "╰") == [4]  # 三行内容：框长到 5 行
    assert rows_with("x" * 200, "╰") == [4]  # 一行很长、自动折成 3 行：框也跟着长

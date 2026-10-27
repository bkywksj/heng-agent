"""第二档（第一批）：紧凑的工具显示、/compact 要求、/copy、/export、/context、/diff、任务完成提醒。"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.ui import session_tools
from heng_agent.ui.repl import Repl, brief_result
from heng_agent.ui.session_tools import context_rows, export_markdown, last_reply, session_diff


def test_brief_result_per_tool() -> None:
    assert brief_result("read_file", "1\ta\n2\tb\n3\tc", False) == ["3 行"]
    assert brief_result("read_file", "（空文件）", False) == ["（空文件）"]
    assert brief_result("glob", "a.py\nb.py", False) == ["2 个"]
    assert brief_result("grep", "a.py:3: x\nb.py:9: y\nc.py:1: z", False) == ["3 处匹配"]
    assert brief_result("grep", "没有匹配", False) == ["没有匹配"]
    assert brief_result("edit_file", "已修改 cart.py（替换 1 处）", False) == ["已修改 cart.py（替换 1 处）"]
    assert brief_result("web_fetch", "x" * 3200, False) == ["读到 3200 字"]
    out = brief_result("bash", "\n".join(f"第{i}行" for i in range(10)), False)
    assert out[:2] == ["第0行", "第1行"] and out[-1] == "…（共 10 行）"  # 命令输出留前几行
    assert brief_result("read_file", "文件不存在：x.py", True) == ["文件不存在：x.py"]  # 出错照原样显示


def test_compact_instruction_reaches_summary_request(tmp_path: Path) -> None:
    model = FakeModel([["一"], ["二"], ["这是摘要"]])
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), model)
    agent.run("第一问", lambda e: None, lambda t, a: "deny")
    agent.run("第二问", lambda e: None, lambda t, a: "deny")
    assert agent.compact(lambda e: None, force=True, instruction="只保留和数据库迁移有关的内容")
    request = model.calls[-1][0]["content"][0]["text"]
    assert "只保留和数据库迁移有关的内容" in request and "优先满足" in request


def test_copy_sends_utf16_to_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[bytes] = []

    def fake_run(cmd: list[str], input: bytes, **kw: Any) -> None:
        sent.append(input)

    monkeypatch.setattr(session_tools.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, _ = session_tools.copy_text("中文和 English ✓")
    assert ok and sent[0].decode("utf-16") == "中文和 English ✓"  # 带 BOM 的 UTF-16：clip 不会把中文弄成乱码


def _conversation() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": [{"type": "text", "text": "修一下 cart.py"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "我先看看"},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "cart.py"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 5000}]},
        {"role": "assistant", "content": [{"type": "text", "text": "改好了，运费门槛是 99 元。"}]},
    ]  # fmt: skip


def test_export_and_last_reply() -> None:
    md = export_markdown(_conversation(), "修购物车")
    assert md.startswith("# 修购物车") and "## 🧑 提问" in md and "修一下 cart.py" in md
    assert "**调用工具 `read_file`**" in md and "后面省略" in md  # 工具结果截断
    assert md.count("## 🧑 提问") == 1  # 只有工具结果的那条不算一次提问
    assert last_reply(_conversation()) == "改好了，运费门槛是 99 元。"


def test_context_rows_split_real_total() -> None:
    rows = dict(context_rows("系统" * 500, [{"name": "t", "description": "d" * 1000}], _conversation(), 10_000))
    assert sum(rows.values()) == pytest.approx(10_000, abs=5)  # 各部分加起来等于模型报的真实总数
    assert rows["工具调用与结果"] > rows["你的消息"]  # 5000 字的工具结果占大头
    with_image = [*_conversation(), {"role": "user", "content": [{"type": "image", "source": {}}]}]
    assert "图片（1 张）" in dict(context_rows("s", [], with_image, 0))  # 没有真实总数时也能粗估


def test_session_diff_and_undo(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    (work / "a.py").write_text("old\n", encoding="utf-8")
    model = FakeModel([[("tool", "write_file", {"path": "a.py", "content": "new\n"}),
                        ("tool", "write_file", {"path": "b.py", "content": "b\n"})], ["好"]])  # fmt: skip
    agent = Agent(Settings(workdir=work, home=tmp_path / "h", permission_mode="auto"), model)
    agent.run("改", lambda e: None, lambda t, a: "once")
    diffs = dict(session_diff(agent.originals, work))
    assert set(diffs) == {"a.py", "b.py"}
    assert "-old" in diffs["a.py"] and "+new" in diffs["a.py"] and "--- /dev/null" in diffs["b.py"]  # 新建的文件
    agent.undo()
    assert session_diff(agent.originals, work) == []  # 撤销后没有改动了


def test_notify_after_long_task(tmp_path: Path) -> None:
    def rang(seconds: float, notify_after: float) -> bool:
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True)
        settings = Settings(workdir=tmp_path, home=tmp_path / "h", notify_after=notify_after)
        Repl(Agent(settings, FakeModel([])), console=console).notify_if_long(seconds)
        return "\a" in buf.getvalue()

    assert rang(45, 30) and not rang(10, 30) and not rang(999, 0)  # 0 = 关闭


def test_slash_commands_run(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    model = FakeModel([["回复内容"]])
    console = Console(record=True, width=120, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), model), console=console)
    repl.agent.run("问", lambda e: None, lambda t, a: "deny")
    repl.handle_command("/export")
    exported = list((work / ".heng-agent" / "exports").glob("*.md"))
    assert len(exported) == 1 and "回复内容" in exported[0].read_text(encoding="utf-8")
    repl.handle_command("/context")
    repl.handle_command("/diff")
    text = console.export_text()
    assert "上下文" in text and "系统提示词" in text and "还没有改过文件" in text


# ---------- 第二批：Ctrl+R / Ctrl+G / Ctrl+T、脚本模式 ----------


def _box(tmp_path: Path, **kw: Any) -> tuple[Any, Any]:
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from heng_agent.ui.inputbox import InputBox
    from heng_agent.ui.prompt import MODES, STYLE, SlashCompleter

    history = InMemoryHistory()
    for line in kw.pop("history", []):
        history.append_string(line)
    pipe = create_pipe_input().__enter__()
    box = InputBox(history=history, commands=SlashCompleter(lambda: {}), workdir=tmp_path, toolbar=lambda: "",
                   get_mode=lambda: "ask", set_mode=lambda m: None, modes=MODES, style=STYLE, input=pipe,
                   output=DummyOutput(), **kw)  # fmt: skip
    return box, pipe


def test_ctrl_r_searches_history(tmp_path: Path) -> None:
    import threading

    box, pipe = _box(tmp_path, history=["git status", "pytest -q", "npm run build"])
    threading.Timer(0.4, pipe.send_text, ["\x12pyte"]).start()  # 历史异步加载完再搜：Ctrl+R 后输入关键词
    threading.Timer(0.8, pipe.send_text, ["\r"]).start()  # 第一次 Enter：采用搜到的（不直接发送）
    threading.Timer(1.1, pipe.send_text, [" --lf\r"]).start()  # 接着改一改再发送
    assert box.prompt() == "pytest -q --lf"


def test_ctrl_g_opens_editor(tmp_path: Path) -> None:
    seen: list[str] = []

    def editor(text: str) -> str:
        seen.append(text)
        return "在编辑器里写的\n多行长消息"

    box, pipe = _box(tmp_path, on_editor=editor)
    pipe.send_text("草稿\x07")  # Ctrl+G
    import threading

    threading.Timer(0.5, pipe.send_text, ["\r"]).start()
    assert box.prompt() == "在编辑器里写的\n多行长消息" and seen == ["草稿"]


def test_todo_box_above_input_and_ctrl_t(tmp_path: Path) -> None:
    import asyncio

    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition

    box, _ = _box(tmp_path, todos=lambda: "任务清单（1/3 完成）\n☑ 读代码\n▶ 修 bug\n☐ 补测试")

    def rows_with(char: str) -> list[int]:
        root, _ = box.build_layout(Buffer(multiline=True))
        screen = Screen()

        async def render() -> None:
            root.write_to_screen(screen, MouseHandlers(), WritePosition(0, 0, 80, 40), "", True, None)

        asyncio.run(render())
        return [y for y in range(40) if any(c.char == char for c in screen.data_buffer[y].values())]

    assert rows_with("▶") == [2] and rows_with("╭") == [4]  # 清单在输入框正上方
    box.show_todos = False  # Ctrl+T 隐藏
    assert rows_with("▶") == [] and rows_with("╭") == [0]


def test_split_rules() -> None:
    from heng_agent.cli import split_rules

    assert split_rules(["bash(npm test),edit_file", "bash(git log --format=%h,%s)"]) == [
        "bash(npm test)", "edit_file", "bash(git log --format=%h,%s)",
    ]  # fmt: skip


def _print_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: list[Any], *argv: str) -> Any:
    from heng_agent import cli

    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    monkeypatch.setenv("HENG_AGENT_HOME", str(tmp_path / "home"))
    model = FakeModel(script)
    monkeypatch.setattr(cli, "make_client", lambda profile, max_tokens: model)
    code = cli.main(["--workdir", str(work), *argv])
    return code, model


def test_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = _print_mode(tmp_path, monkeypatch, [["答案是 42"]], "-p", "问", "--output-format", "json")
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["result"] == "答案是 42" and out["is_error"] is False and out["session_id"]


def test_stream_json_and_allowed_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    script = [[("tool", "bash", {"command": "echo hi"})], ["跑完了"]]
    _print_mode(tmp_path, monkeypatch, script, "-p", "跑", "--output-format", "stream-json",
                "--allowed-tools", "bash(echo hi)")  # fmt: skip
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "tool_start" and kinds[-1] == "done"
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["denied"] is False and "hi" in end["content"]  # --allowed-tools 放行了，-p 下也能跑
    capsys.readouterr()
    _print_mode(tmp_path, monkeypatch, script, "-p", "跑", "--output-format", "stream-json")
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert next(e for e in events if e["type"] == "tool_end")["denied"] is True  # 不放行：没人可问，拒绝


def test_piped_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from heng_agent import cli

    class Piped(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(cli.sys, "stdin", Piped("Traceback: ZeroDivisionError"))
    _, model = _print_mode(tmp_path, monkeypatch, [["是除零"]], "-p", "分析这段错误")
    sent = model.calls[0][0]["content"][0]["text"]
    assert sent.startswith("分析这段错误") and "管道传进来的内容" in sent and "ZeroDivisionError" in sent
    monkeypatch.setattr(cli.sys, "stdin", Piped("只有管道内容"))
    _, model = _print_mode(tmp_path, monkeypatch, [["好"]], "-p")
    assert model.calls[0][0]["content"][0]["text"] == "只有管道内容"  # 只写 -p：管道内容就是问题

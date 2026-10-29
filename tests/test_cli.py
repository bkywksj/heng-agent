"""命令行与界面：-p 非交互模式的权限行为、--resume、斜杠命令、事件渲染与确认对话。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent import cli
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import TextDelta, ToolFinished, ToolResult, ToolStarted, TurnDone
from heng_agent.ui import repl as repl_module
from heng_agent.ui.repl import Repl


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    monkeypatch.delenv("HENG_AGENT_HOME", raising=False)  # 用「家目录下的默认数据目录」
    monkeypatch.setenv(
        "HENG_AGENT_CHANNEL", "release"
    )  # 按正式版算：~/.heng-agent  # 会话写到临时目录，不碰真实用户目录
    scripts: list[FakeModel] = []
    monkeypatch.setattr(cli, "make_client", lambda profile, max_tokens: scripts.pop(0))
    return {"work": work, "scripts": scripts}


def test_print_mode_denies_edits_by_default(env: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    edit = ("tool", "write_file", {"path": "a.txt", "content": "x"})
    env["scripts"].append(FakeModel([[edit], ["没权限，先不改了"]]))
    assert cli.main(["-p", "建个文件", "--workdir", str(env["work"])]) == 0
    out = capsys.readouterr()
    assert not (env["work"] / "a.txt").exists() and "已拒绝" in out.err and "没权限" in out.out


def test_print_mode_with_accept_edits_writes(env: dict[str, Any]) -> None:
    env["scripts"].append(FakeModel([[("tool", "write_file", {"path": "a.txt", "content": "x"})], ["好了"]]))
    assert cli.main(["-p", "建", "--workdir", str(env["work"]), "--permission-mode", "accept-edits"]) == 0
    assert (env["work"] / "a.txt").read_text(encoding="utf-8") == "x"


def test_resume_continues_latest_session(env: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    env["scripts"].append(FakeModel([["记住了"]]))
    cli.main(["-p", "项目叫衡", "--workdir", str(env["work"])])
    second = FakeModel([["你说项目叫衡"]])
    env["scripts"].append(second)
    cli.main(["-p", "项目叫什么", "--workdir", str(env["work"]), "--resume"])
    assert "已接上会话" in capsys.readouterr().err
    first_question = second.calls[0][0]["content"][0]["text"]
    assert first_question == "项目叫衡" and len(second.calls[0]) == 3  # 上次的一问一答 + 这次的问题


def _repl(tmp_path: Path) -> tuple[Repl, Console]:
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    agent = Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([]))
    console = Console(record=True, width=100, force_terminal=False)
    return Repl(agent, console=console), console


def test_slash_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repl, console = _repl(tmp_path)
    assert repl.handle_command("/mode plan") and repl.agent.permissions.mode == "plan"
    repl.handle_command("/model x")  # 假模型没有配置档：提示不支持，而不是崩溃
    repl.handle_command("/provider deepseek")
    assert repl.agent.client.profile.base_url == "https://api.deepseek.com/anthropic"
    monkeypatch.setattr(repl, "_ask", lambda prompt="": "n")  # 切模型后问「也存成默认吗」：不存
    repl.handle_command("/model deepseek-flash")
    assert repl.agent.client.profile.model == "deepseek-flash"
    repl.handle_command("/provider")
    repl.agent.messages.append({"role": "user", "content": "x"})
    repl.handle_command("/clear")
    assert repl.agent.messages == []
    repl.handle_command("/nope")
    assert repl.handle_command("/exit") is False
    text = console.export_text()
    assert "权限模式：计划模式（plan）" in text and "不支持切换模型名" in text and "已切换到 deepseek" in text
    assert "可选的服务商" in text and "已开始新会话" in text and "不认识的命令" in text


def test_event_rendering(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    repl.on_event(TextDelta("看一下 [不是标记]"))  # 方括号原样显示，不被当成 rich 标记吃掉
    repl.on_event(ToolStarted("t1", "bash", {"command": "pytest -q"}))
    repl.on_event(ToolFinished("t1", "bash", ToolResult("\n".join(f"第{i}行" for i in range(10)), is_error=True)))
    repl.on_event(ToolFinished("t2", "write_file", ToolResult(""), denied=True))
    repl.on_event(TurnDone({"input_tokens": 3, "output_tokens": 4}, rounds=2))
    text = console.export_text()
    assert "[不是标记]" in text and "● 运行 pytest -q" in text and "第2行" in text and "第3行" not in text
    lines = text.splitlines()
    assert any(line.startswith("● 运行 pytest -q") for line in lines)  # 工具行另起一行，不接在 AI 回复后面
    assert "共 10 行" in text and "已拒绝" in text and "2 轮" in text


def test_approve_shows_diff_and_parses_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repl, console = _repl(tmp_path)
    # 测的是「选择器用不了时退回打字」：明确指定，别依赖环境（Windows CI 上恰好用不了，Linux 上用得了、走了另一条路）
    monkeypatch.setattr(repl, "_choose", lambda *a, **kw: repl_module._PLAIN)
    answers = iter(["随便", "a"])
    monkeypatch.setattr(console, "input", lambda prompt="": next(answers))
    tool = repl.agent.tools["edit_file"]
    result = repl.approve(tool, {"path": "x.py", "old_string": "a = 1", "new_string": "a = 2"})
    assert result == "always"  # 无效回答会重问，直到给出 y / a / n
    text = console.export_text()
    assert "-a = 1" in text and "+a = 2" in text


def test_input_falls_back_when_no_native_console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 测试环境（以及 Git Bash 的 mintty）没有原生 Windows 控制台：不能崩，要退回普通逐行输入
    repl, console = _repl(tmp_path)
    lines = iter(["第一行\\", "第二行"])
    monkeypatch.setattr(console, "input", lambda prompt="": next(lines))
    monkeypatch.setattr("heng_agent.ui.repl.InputBox", lambda **kw: (_ for _ in ()).throw(RuntimeError("no console")))
    assert repl.read() == "第一行\n第二行" and repl._plain_input


def test_streamed_markdown_is_rendered(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    for piece in ["# 修复说明\n\n- 第一", "处\n- 第二处\n\n```python\nprint(", "'ok')\n```\n"]:  # 分段到达，标记被切开
        repl.on_event(TextDelta(piece))
    repl.on_event(TurnDone({"input_tokens": 1, "output_tokens": 1}, rounds=1))
    text = console.export_text()
    assert "修复说明" in text and "第一处" in text and "第二处" in text and "print('ok')" in text
    assert "```" not in text and "# 修复说明" not in text  # 按 Markdown 渲染，不是原样输出标记


def test_compact_command_and_notices(tmp_path: Path) -> None:
    from heng_agent.core.types import Notice

    repl, console = _repl(tmp_path)
    repl.agent.client = FakeModel([["摘要：之前在修 bug"]])
    repl.agent.messages = [
        {"role": "user", "content": [{"type": "text", "text": "修 bug"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "好的"}]},
        {"role": "user", "content": [{"type": "text", "text": "继续"}]},
    ]
    repl.handle_command("/compact")
    assert "工作摘要" in repl.agent.messages[0]["content"][0]["text"]
    repl.on_event(Notice("模型服务返回 529；2 秒后重试"))
    text = console.export_text()
    assert "已压缩" in text and "529" in text


def test_choose_without_terminal_falls_back_to_typing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：标准输入不是终端（脚本、管道、CI）时不能开选择器——选择器读到「输入结束」会当成按了 ESC 取消，
    CI 上第一次在 Linux / macOS 跑，配置向导就这样被当成取消、抛出中断，整轮测试被打断。"""
    repl, _ = _repl(tmp_path)

    def must_not_open(*a: Any, **kw: Any) -> str:
        raise AssertionError("标准输入不是终端时不该开选择器")

    monkeypatch.setattr(repl_module, "choose", must_not_open)
    monkeypatch.setattr(repl_module, "_stdin_is_terminal", lambda: False)
    assert repl._choose("选一个", []) is repl_module._PLAIN


def test_session_id_preallocated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """--session-id：调用方（如智码）预先分配会话 id，不用事后猜刚开的是哪个会话。"""
    from heng_agent.session import SessionStore

    work = tmp_path / "w"
    work.mkdir()
    home = Path(os.environ["HENG_AGENT_HOME"])
    monkeypatch.setattr(cli, "make_client", lambda profile, max_tokens: FakeModel([["好"]]))
    sid = "zhima-3f2a9c1e-7b4d"
    assert cli.main(["--workdir", str(work), "--session-id", sid, "-p", "你好"]) == 0
    assert SessionStore(home, work, sid).path.is_file()  # 会话文件就叫这个 id
    capsys.readouterr()
    assert cli.main(["--workdir", str(work), "--session-id", sid, "-p", "再来"]) == 1
    assert f"--resume {sid}" in capsys.readouterr().err  # 已在用：报错，不悄悄接到旧会话上
    assert cli.main(["--workdir", str(work), "--session-id", "../../逃出去", "-p", "x"]) == 1
    assert "不合法" in capsys.readouterr().err
    assert not (home.parent / "逃出去.json").exists() and not list(tmp_path.rglob("逃出去.json"))
    assert cli.main(["--workdir", str(work), "--session-id", "abc", "--resume", sid, "-p", "x"]) == 1
    assert "不能和 --resume" in capsys.readouterr().err


def test_initial_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """heng-agent "要做的事"：进入交互界面并先发出这句话（智码「带一句话新建会话」这样启动）。"""
    from heng_agent.agent import Agent
    from heng_agent.core.config import Settings
    from heng_agent.ui.repl import Repl

    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([]))
    assert Repl(agent, initial_prompt="把包邮门槛改成 88")._queued == ["把包邮门槛改成 88"]  # 欢迎页之后先发它
    assert Repl(agent)._queued == []
    # heng-agent "问题" -p：这句话当 -p 的问题
    model = FakeModel([["好"]])
    monkeypatch.setattr(cli, "make_client", lambda profile, max_tokens: model)
    work = tmp_path / "w"
    work.mkdir()
    assert cli.main(["--workdir", str(work), "问一句", "-p"]) == 0
    assert "问一句" in str(model.calls[0])


def test_clear_keeps_pinned_session_id(tmp_path: Path) -> None:
    """--session-id / --resume <id> 指定的会话：/clear 时 id 不变（智码按 id 记着标签页），旧对话另存成新 id。"""
    import json as _json

    from heng_agent.agent import Agent
    from heng_agent.core.config import Settings
    from heng_agent.session import SessionStore
    from heng_agent.ui.repl import Repl

    home = tmp_path / "h"
    agent = Agent(Settings(workdir=tmp_path, home=home), FakeModel([]))
    pinned = SessionStore(home, tmp_path, "zhima-tab-1")
    pinned.pinned = True
    pinned.title = "包邮改造"
    agent.session = pinned
    agent.messages = [{"role": "user", "content": [{"type": "text", "text": "把门槛改成 88"}]}]
    pinned.save(agent.messages, agent.usage)
    Repl(agent).handle_command("/clear")
    assert agent.session is pinned and agent.session.id == "zhima-tab-1"  # id 不变
    assert not pinned.path.exists() and agent.messages == [] and pinned.title == ""
    archived = [s for s in SessionStore.all(home, tmp_path) if s.id != "zhima-tab-1"]
    assert len(archived) == 1 and archived[0].first_question == "把门槛改成 88"  # 旧对话还在，能 --resume 找回
    assert _json.loads((pinned.dir / f"{archived[0].id}.json").read_text(encoding="utf-8"))["title"] == "包邮改造"

    plain = Agent(Settings(workdir=tmp_path, home=home), FakeModel([]))
    before = plain.session.id if plain.session else None
    Repl(plain).handle_command("/clear")
    assert plain.session is not None and plain.session.id != before  # 没指定 id 的：照旧开新会话


def test_pinned_flag_from_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from heng_agent.core.config import Settings

    settings = Settings(workdir=tmp_path, home=tmp_path / "h")
    store = cli.new_session_store(settings, "zhima-tab-2", resuming=False)
    assert store is not None and store.pinned  # --session-id：固定
    assert cli.new_session_store(settings, None, resuming=False) is None


def test_no_session_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """--no-session-persistence：一次性的后台调用（智码生成提交信息等）不留会话记录。"""
    from heng_agent.session import SessionStore

    work = tmp_path / "w"
    work.mkdir()
    home = Path(os.environ["HENG_AGENT_HOME"])
    monkeypatch.setattr(cli, "make_client", lambda profile, max_tokens: FakeModel([["feat: 加功能"]]))
    assert cli.main(["--workdir", str(work), "--no-session-persistence", "-p", "生成提交信息"]) == 0
    assert "feat: 加功能" in capsys.readouterr().out
    assert SessionStore.all(home, work) == []  # 没有留下会话文件
    assert cli.main(["--workdir", str(work), "--no-session-persistence", "--resume", "x", "-p", "y"]) == 1
    assert "不保存会话" in capsys.readouterr().err

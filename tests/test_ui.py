"""界面外观：欢迎页、MCP 连接状态、斜杠命令补全、底部状态栏、Shift+Tab 切换权限模式。"""

from __future__ import annotations

from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import Console

from fake_model import FakeModel
from heng_agent import __version__
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.ui.prompt import MODES, SlashCompleter, toolbar
from heng_agent.ui.repl import Repl


def _completions(text: str, custom: dict[str, str] | None = None) -> dict[str, str]:
    comp = SlashCompleter(lambda: custom or {})
    found = comp.get_completions(Document(text, len(text)), CompleteEvent())
    return {c.text: c.display_meta_text for c in found}


def test_slash_completion() -> None:
    all_cmds = _completions("/", {"addtest": "为指定函数补单元测试", "help": "不该覆盖内置说明"})
    assert "/undo" in all_cmds and all_cmds["/addtest"] == "为指定函数补单元测试"
    assert all_cmds["/help"] == "显示命令说明"  # 内置优先
    assert set(_completions("/co")) == {"/compact", "/config", "/context", "/copy", "/cost"}
    assert _completions("普通输入 /co") == {} and _completions("/mode a") == {}  # 只补全行首的命令名


def test_toolbar_content() -> None:
    def text(ft: object) -> str:
        return "".join(part[1] for part in ft)  # type: ignore[attr-defined]

    bar = toolbar("ask", "yongye · claude-sonnet-5", r"E:\proj", 1741, 200_000)
    assert (
        "ask 每次问" in text(bar) and "yongye · claude-sonnet-5" in text(bar) and "上下文 1%（1.7k/200k）" in text(bar)
    )
    hot = toolbar("auto", "m", "w", 170_000, 200_000)
    styles = [part[0] for part in hot]
    assert "class:bar.mode-auto" in styles and "class:bar.ctx-hot" in styles  # auto 与上下文快满都醒目标出
    long_dir = text(toolbar("ask", "m", "C:/" + "x" * 80, 0, 1))
    assert "…" in long_dir  # 太长的目录从左边截断


def test_mode_cycle_order() -> None:
    assert MODES == ["ask", "accept-edits", "plan", "auto"]


def test_banner_and_mcp_status(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    agent = Agent(Settings(workdir=work, home=tmp_path / "h", permission_mode="auto"), FakeModel([]))
    console = Console(record=True, width=140, force_terminal=False)
    status = [("calc", True, "2 tools (stdio)"), ("broken", False, "启动 MCP 服务 broken 失败：找不到文件")]
    repl = Repl(agent, console=console, provider_name="yongye", mcp_status=status)
    repl.banner()
    text = console.export_text()
    assert "欢迎使用 heng-agent" in text and str(work) in text and __version__ in text
    assert "yongye" in text and "auto 全部不问" in text and "1 个已连接" in text
    assert "MCP：1 个服务已连接 · 2 个工具" in text  # 都连上的只一行汇总，不刷屏
    assert 'MCP 服务 "broken" 连接失败：启动 MCP 服务 broken 失败' in text  # 连不上的单独列、写原因
    assert "calc" not in text.split("MCP：")[1].splitlines()[0]  # 汇总行里不再逐个列名字
    assert "Shift+Tab" in text  # 提示行


@pytest.mark.parametrize(("dev", "sgr"), [(False, "36"), (True, "35")])
def test_banner_logo_color_by_channel(tmp_path: Path, monkeypatch, dev: bool, sgr: str) -> None:  # type: ignore[no-untyped-def]
    from heng_agent.ui import repl as repl_mod

    monkeypatch.setattr(repl_mod, "is_dev", lambda: dev)
    work = tmp_path / "w"
    work.mkdir()
    console = Console(record=True, width=140, force_terminal=True, color_system="standard")
    repl_mod.Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([])), console=console).banner()
    styled = console.export_text(styles=True)
    for line in repl_mod.LOGO:  # 方块字标两行都在，正式版青色（36）、开发版品红（35）
        assert f"\x1b[1;{sgr}m{line}" in styled
    assert ("开发版" in styled) is dev


def test_spinner_follows_events_and_stops_before_asking(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from heng_agent.core.types import TextDelta, ToolFinished, ToolResult, ToolStarted, TurnDone

    work = tmp_path / "w"
    work.mkdir()
    repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([])),
                console=Console(record=True, width=100, force_terminal=False))  # fmt: skip
    repl._spin("思考中…")
    assert repl._status is not None
    repl.on_event(TextDelta("好的"))
    assert repl._status is None  # 文字开始流出：动画让位给 Markdown 区域
    repl.on_event(ToolStarted("t", "bash", {"command": "pytest"}))
    assert repl._status is not None  # 执行工具时转圈
    monkeypatch.setattr(repl.console, "input", lambda prompt="": "n")
    repl.approve(repl.agent.tools["bash"], {"command": "pytest"})
    assert repl._status is None  # 问用户之前先停动画
    repl.on_event(ToolFinished("t", "bash", ToolResult("退出码 0")))
    assert repl._status is not None  # 结果交回模型，又在等模型
    repl.on_event(TurnDone({}, rounds=1))
    assert repl._status is None


def test_user_message_is_echoed_with_marker(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    console = Console(record=True, width=100, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([])), console=console)
    repl.echo_user("第一行\n第二行 [不是标记]")
    assert console.export_text() == "❯ 第一行\n  第二行 [不是标记]\n\n"  # 末尾空一行：和下面的「思考中」拉开


def test_attachments_render_as_hyperlinks(tmp_path: Path) -> None:
    """指向真实文件的 @引用 / 绝对路径渲染成 OSC 8 超链接：智码终端据此悬停预览，Windows Terminal 里 Ctrl+点击打开。"""
    import io

    from heng_agent.ui.repl import linkify

    work = tmp_path / "w"
    (work / "att").mkdir(parents=True)
    shot = work / "att" / "shot.png"
    shot.write_bytes(b"png")
    markup = linkify(f"看 @att/shot.png 和 {shot}，@nope.png 与 [不是标记]", work)
    assert markup.count("[link=file:///") == 2 and "nope.png" in markup
    buf = io.StringIO()
    # legacy_windows=False：rich 在 Windows 上认不出终端时按老式控制台处理（不输出超链接）；
    # 真实运行在 Windows Terminal 里能正确识别为支持 VT 的终端
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=200, legacy_windows=False)
    console.print(markup)
    out = buf.getvalue()
    assert "\x1b]8;" in out and shot.resolve().as_uri() in out  # 真的输出了超链接序列，指向那个文件
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)  # 去掉颜色码（rich 默认会给方括号加粗）再比文字
    assert "[不是标记]" in plain  # 方括号文字原样显示，没被当成 rich 标记吃掉


def test_enable_vt_is_safe_without_console() -> None:
    # 测试里标准输出被捕获、不是控制台：GetConsoleMode 失败，应当什么都不做而不是报错
    from heng_agent import cli

    cli._enable_vt()


# ---------- 流式回复分段固定、网址可点 ----------


def test_split_done_never_cuts_code_blocks() -> None:
    from heng_agent.ui.repl import split_done

    assert split_done("第一段\n\n第二段还在写") == ("第一段", "第二段还在写")
    assert split_done("还没写完一段") == ("", "还没写完一段")
    code = "说明\n\n```python\nx = 1\n\ny = 2"  # 代码块里有空行、还没闭合：只能切在代码块之前
    assert split_done(code) == ("说明", "```python\nx = 1\n\ny = 2")
    closed = "说明\n\n```python\nx = 1\n\ny = 2\n```\n\n结尾"
    assert split_done(closed)[0].endswith("```") and split_done(closed)[1] == "结尾"


def test_long_streamed_reply_prints_each_paragraph_once(tmp_path: Path) -> None:
    """回复长过一屏时，整段重绘会把滚出屏幕的开头一遍遍重印（实测重复十几次）。分段固定后每段只打一次。"""
    from heng_agent.core.types import TextDelta, TurnDone

    console = Console(record=True, width=80, height=10, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([])), console=console)
    reply = "\n\n".join(f"第 {i} 段：Sigil 中与 Git 相关的统计信息" for i in range(30))
    for i in range(0, len(reply), 7):  # 一小截一小截地流进来
        repl.on_event(TextDelta(reply[i : i + 7]))
    repl.on_event(TurnDone({}, rounds=1))
    text = console.export_text()
    for i in range(30):
        assert text.count(f"第 {i} 段：") == 1, i
    assert text.count("✦") == 1  # 一条回复只有开头一个 ✦


def test_spinner_resumes_after_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """允许之后工具可能要跑好几秒：等待动画要马上重新打开，否则屏幕没动静、看着像卡住了。"""
    from heng_agent.tools.base import builtin_tools

    repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([])),
                console=Console(record=True, width=100, force_terminal=False))  # fmt: skip
    bash = {t.name: t for t in builtin_tools()}["bash"]
    monkeypatch.setattr(repl.console, "input", lambda prompt="": "y")  # 选择器用不了：退回打字，回答允许
    assert repl.approve(bash, {"command": "pytest"}) == "once"
    assert repl._status is not None  # 允许后动画在转
    repl._unspin()
    monkeypatch.setattr(repl.console, "input", lambda prompt="": "n")
    assert repl.approve(bash, {"command": "rm x"}) == "deny"
    assert repl._status is None  # 拒绝了就不用转


def test_autolink() -> None:
    import io

    from heng_agent.ui.repl import autolink

    assert autolink("百度：https://www.baidu.com。") == "百度：<https://www.baidu.com>。"  # 中文句号不算进网址
    assert autolink("见 https://a.com/x.") == "见 <https://a.com/x>."
    assert autolink("[文档](https://a.com) 和 <https://b.com>") == "[文档](https://a.com) 和 <https://b.com>"
    assert autolink("跑 `curl https://a.com`") == "跑 `curl https://a.com`"  # 行内代码不动
    fenced = "```\ncurl https://a.com\n```"
    assert autolink(fenced) == fenced  # 代码块不动
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100, legacy_windows=False)
    console.print(Repl._reply("百度地址：https://www.baidu.com"))
    assert "\x1b]8;" in buf.getvalue() and "https://www.baidu.com" in buf.getvalue()  # 渲染成超链接

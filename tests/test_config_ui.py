"""这一轮的界面与配置：k/M、Shift+Enter、拖入与粘贴、Alt+V、/config、settings 段、AI 回复标识。"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from heng_agent import cli, configfile
from heng_agent.agent import Agent
from heng_agent.attachments import ATTACH_DIR, paste_to_mentions, paths_in_paste
from heng_agent.core.config import Settings
from heng_agent.core.errors import AgentError
from heng_agent.model.client import make_client
from heng_agent.model.providers import load_profiles
from heng_agent.ui import config_cmd
from heng_agent.ui.inputbox import InputBox
from heng_agent.ui.prompt import MODES, STYLE, SlashCompleter, human_tokens
from heng_agent.ui.repl import Repl
from heng_agent.ui.winkeys import SHIFT_PRESSED, VK_RETURN, is_shift_enter


def test_human_tokens() -> None:
    assert [human_tokens(n) for n in (0, 950, 1741, 15_000, 200_000, 1_000_000, 1_250_000)] == [
        "0", "950", "1.7k", "15k", "200k", "1M", "1.2M",
    ]  # fmt: skip


def test_shift_enter_detection() -> None:
    ev = SimpleNamespace(KeyDown=True, VirtualKeyCode=VK_RETURN, ControlKeyState=SHIFT_PRESSED)
    assert is_shift_enter(ev)
    assert not is_shift_enter(SimpleNamespace(KeyDown=True, VirtualKeyCode=VK_RETURN, ControlKeyState=0))
    assert not is_shift_enter(SimpleNamespace(KeyDown=False, VirtualKeyCode=VK_RETURN, ControlKeyState=SHIFT_PRESSED))


# ---------- 拖入与粘贴 ----------


def test_paste_paths_become_mentions(tmp_path: Path) -> None:
    work, outside = tmp_path / "proj", tmp_path / "外面 的目录"
    work.mkdir()
    outside.mkdir()
    (work / "app.py").write_text("x", encoding="utf-8")
    shot = outside / "截图 1.png"
    shot.write_bytes(b"png")
    # 拖入工作目录里的文件：直接引用
    assert paste_to_mentions(str(work / "app.py"), work) == "@app.py "
    # 拖入外部、带空格的文件（Windows Terminal 会加引号）：复制进附件目录再引用
    got = paste_to_mentions(f'"{shot}"', work)
    assert got is not None and got.startswith(f"@{ATTACH_DIR.as_posix()}/") and got.endswith(".png ")
    copied = work / got.strip()[1:]
    assert copied.read_bytes() == b"png" and " " not in copied.name
    assert (work / ATTACH_DIR / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")  # 附件不进仓库
    # 一次拖入两个
    assert paste_to_mentions(f'{work / "app.py"} "{shot}"', work).count("@") == 2  # type: ignore[union-attr]
    # 普通文字、不存在的路径、路径混着文字：按普通粘贴
    assert paste_to_mentions("修一下这个 bug", work) is None
    assert paste_to_mentions(str(work / "nope.py"), work) is None
    assert paths_in_paste(f"看看 {work / 'app.py'}") is None


#: 留住管道的上下文对象：只拿 __enter__() 的返回值的话，函数一返回上下文对象就被回收、管道随之关掉——
#: Windows 上的模拟输入关了还能写，Linux / macOS 是真的管道，再写就报 Bad file descriptor（CI 上踩过）
_OPEN_PIPES: list[Any] = []


def _box(tmp_path: Path, **kw: Any) -> tuple[InputBox, Any]:
    pipe_ctx = create_pipe_input()
    _OPEN_PIPES.append(pipe_ctx)
    pipe = pipe_ctx.__enter__()
    box = InputBox(history=InMemoryHistory(), commands=SlashCompleter(lambda: {}), workdir=tmp_path,
                   toolbar=lambda: "", get_mode=lambda: "ask", set_mode=lambda m: None, modes=MODES, style=STYLE,
                   input=pipe, output=DummyOutput(), **kw)  # fmt: skip
    return box, pipe


def test_bracketed_paste_and_alt_v_in_input_box(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    box, pipe = _box(tmp_path, on_paste=lambda t: paste_to_mentions(t, tmp_path))
    pipe.send_text(f"看 \x1b[200~{tmp_path / 'a.py'}\x1b[201~" + "\r")  # 终端的括号粘贴 = 拖入文件
    assert box.prompt() == "看 @a.py "
    assert "@ 引用" in box.flash
    box, pipe = _box(tmp_path, on_paste=lambda t: paste_to_mentions(t, tmp_path))
    pipe.send_text("\x1b[200~第一行\r\n第二行\x1b[201~\r")  # 普通多行文字原样粘贴
    assert box.prompt() == "第一行\n第二行"
    box, pipe = _box(tmp_path, on_clipboard=lambda: ("@shot.png ", "已粘贴图片 shot.png"))
    pipe.send_text("这张图 ")
    threading.Timer(0.3, pipe.send_text, ["\x1bv\r"]).start()  # Alt+V
    assert box.prompt() == "这张图 @shot.png " and box.flash == "已粘贴图片 shot.png"


# ---------- 配置文件 ----------


def test_configfile_paths_and_masking() -> None:
    data: dict[str, Any] = {}
    configfile.set_path(data, "profiles.yy.model", "claude-sonnet-5")
    configfile.set_path(data, "profiles.yy.api_key", "sk-1234567890abcd")
    assert configfile.get_path(data, "profiles.yy.model") == "claude-sonnet-5"
    assert configfile.mask(data)["profiles"]["yy"]["api_key"] == "sk-…abcd"
    configfile.unset_path(data, "profiles.yy.model")
    with pytest.raises(AgentError, match="没有"):
        configfile.get_path(data, "profiles.yy.model")
    assert configfile.parse_value("42") == 42 and configfile.parse_value("true") is True
    assert configfile.parse_value('{"a": 1}') == {"a": 1} and configfile.parse_value("plan") == "plan"


def test_settings_section_and_cli_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work = tmp_path / "home", tmp_path / "proj"
    work.mkdir()
    configfile.save(home / "config.json", {"settings": {"permission_mode": "plan", "max_rounds": 7}})
    configfile.save(work / ".heng-agent/config.json", {"settings": {"max_rounds": 9}})
    monkeypatch.setenv("HENG_AGENT_HOME", str(home))
    args = cli.build_parser().parse_args(["--workdir", str(work)])
    s = cli.make_settings(args)
    assert s.permission_mode == "plan" and s.max_rounds == 9  # 项目覆盖用户
    args = cli.build_parser().parse_args(["--workdir", str(work), "--permission-mode", "ask"])
    assert cli.make_settings(args).permission_mode == "ask"  # 命令行优先
    configfile.save(home / "config.json", {"settings": {"colour": "red"}})
    assert "colour" not in configfile.settings_overrides(home, work)  # 启动时：不认识的字段忽略，照样能启动
    assert configfile.unknown_settings(home, work) == ["colour"]  # 但会提示出来
    with pytest.raises(AgentError, match="不认识"):
        configfile.validate_all(home, work)  # 改配置时（/config set）仍严格：多半是打错了字段名


# ---------- /config ----------


def _repl(tmp_path: Path) -> tuple[Repl, Console]:
    home, work = tmp_path / "home", tmp_path / "proj"
    work.mkdir(exist_ok=True)
    profile = {
        "protocol": "anthropic",
        "base_url": "https://relay.example",
        "model": "m1",
        "api_key": "sk-secret-123456",
    }
    configfile.save(home / "config.json", {"default": "yy", "profiles": {"yy": profile}})
    settings = Settings(workdir=work, home=home)
    profiles, _ = load_profiles(home, work)
    agent = Agent(settings, make_client(profiles["yy"], 100))
    console = Console(record=True, width=160, force_terminal=False)
    return Repl(agent, console=console, provider_name="yy"), console


def test_config_show_masks_keys(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    repl.handle_command("/config")
    text = console.export_text()
    assert "yy" in text and "m1" in text and "sk-…3456" in text and "sk-secret-123456" not in text


def test_config_set_applies_live(tmp_path: Path) -> None:
    repl, _ = _repl(tmp_path)
    repl.handle_command("/config set profiles.yy.model m2")
    assert repl.agent.client.profile.model == "m2"  # 不用重启，客户端已换成新模型
    repl.handle_command("/config set settings.permission_mode plan")
    assert repl.agent.permissions.mode == "plan"
    repl.handle_command("/config set default deepseek")
    assert repl.provider_name == "deepseek" and "deepseek" in repl.agent.client.profile.base_url
    saved = configfile.load(repl.agent.settings.home / "config.json")
    assert saved["default"] == "deepseek" and saved["settings"]["permission_mode"] == "plan"


def test_config_rejects_bad_changes_and_restores(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    path = repl.agent.settings.home / "config.json"
    before = path.read_bytes()
    repl.handle_command('/config set profiles.yy.protocol "grpc"')
    repl.handle_command("/config set settings.max_rounds -3")
    repl.handle_command("/config set profiles.p.api_key sk-x --project")  # 项目配置不许写密钥
    assert path.read_bytes() == before
    assert not (repl.agent.settings.workdir / ".heng-agent/config.json").exists()
    assert console.export_text().count("已恢复原配置") == 3


def test_config_provider_wizard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repl, console = _repl(tmp_path)
    # 选择器用不了（测试里不是终端）：退回输编号。直接回车 = 默认项
    answers = iter(["", "", "https://new.example", "relay2", "sk-new-key-0000", "2", "y"])
    # 服务商（默认自定义）→ 协议（默认 anthropic）→ 地址 → 名字 → 密钥 → 第 2 个模型 → 设默认
    asked: list[tuple[str, bool]] = []

    def fake_ask(prompt: str = "", *, password: bool = False) -> str:
        asked.append((prompt, password))
        return next(answers)

    monkeypatch.setattr(repl, "_ask", fake_ask)
    monkeypatch.setattr(config_cmd, "list_models", lambda profile: ["claude-haiku", "claude-sonnet-5"])
    repl.handle_command("/config provider")
    saved = configfile.load(repl.agent.settings.home / "config.json")
    assert saved["default"] == "relay2"
    assert saved["profiles"]["relay2"] == {"protocol": "anthropic", "base_url": "https://new.example",
                                           "api_key": "sk-new-key-0000", "model": "claude-sonnet-5"}  # fmt: skip
    assert repl.provider_name == "relay2" and repl.agent.client.profile.model == "claude-sonnet-5"
    assert "sk-new-key-0000" not in console.export_text()  # 密钥不回显
    assert [pw for p, pw in asked if "密钥" in p] == [True]  # 密钥输入不显示


def test_config_provider_wizard_can_be_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """中途按 ESC / Ctrl+C：只是取消、什么都不保存，程序不退出（以前直接带着堆栈退出了）。"""
    repl, console = _repl(tmp_path)
    before = configfile.load(repl.agent.settings.home / "config.json")
    answers = iter(["", "", "https://new.example"])

    def fake_ask(prompt: str = "", *, password: bool = False) -> str:
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None  # 问到名字时按了 Ctrl+C

    monkeypatch.setattr(repl, "_ask", fake_ask)
    with repl._cancellable():
        repl.handle_command("/config provider")
    assert configfile.load(repl.agent.settings.home / "config.json") == before
    assert "已取消" in console.export_text()


def test_enter_runs_commands_tab_completes(tmp_path: Path) -> None:
    import threading

    from heng_agent.ui.prompt import SlashCompleter

    def run(keys: str, hints: Any = None) -> str:
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        from heng_agent.ui.inputbox import InputBox
        from heng_agent.ui.prompt import MODES, STYLE

        with create_pipe_input() as pipe:
            box = InputBox(history=InMemoryHistory(), commands=SlashCompleter(lambda: {}, hints), workdir=tmp_path,
                           toolbar=lambda: "", get_mode=lambda: "ask", set_mode=lambda m: None, modes=MODES,
                           style=STYLE, input=pipe, output=DummyOutput())  # fmt: skip
            for i, chunk in enumerate(keys.split("|")):
                threading.Timer(0.3 * (i + 1), pipe.send_text, [chunk]).start()
            return box.prompt()

    assert run("/dif|\r") == "/diff"  # 菜单弹出、没选：Enter 直接执行第一项（不会当成未知命令 /dif）
    assert run("/conf|\r") == "/config"  # 参数可选的命令：Enter 直接执行（弹总览 / 选择器）
    assert run("/ren|\r|我的会话\r") == "/rename 我的会话"  # 必须带参数的：Enter 只补全，等你输完
    hints = lambda cmd, prior: [("provider", "引导式", False), ("set", "改字段", True)] if cmd == "config" else []  # noqa: E731
    assert run("/conf|\t|prov|\r", hints) == "/config provider"  # Tab 补全加空格、接着提示参数，选参数后 Enter 执行


def test_assistant_reply_is_indented_without_marker(tmp_path: Path) -> None:
    """回复不加图标、整段缩进两格（提问左侧是竖条，两者一眼分得开）；换行后仍然对齐。"""
    repl, console = _repl(tmp_path)
    console.print(repl._reply("第一段\n\n- 要点"))
    lines = [line for line in console.export_text().splitlines() if line.strip()]
    assert lines[0].rstrip() == "  第一段" and all(line.startswith("  ") for line in lines)  # 表格会补齐行尾空格


# ---------- 发送时吸收绝对路径（智码 Ctrl+V 截图、拖入文件、粘贴标记被吞） ----------


def test_absorb_paths_in_message(tmp_path: Path) -> None:
    from heng_agent.attachments import absorb_paths

    work, temp = tmp_path / "proj", tmp_path / "Temp"
    work.mkdir()
    temp.mkdir()
    shot = temp / "clipboard_img_3f2a.png"  # 智码存截图的样子
    shot.write_bytes(b"png")
    spaced = temp / "需求 文档.md"
    spaced.write_text("内容", encoding="utf-8")
    (work / "app.py").write_text("x", encoding="utf-8")
    text = f'看看这个 {shot} 和 "{spaced}"，还有 {work / "app.py"}'
    out, attached = absorb_paths(text, work)
    assert out.startswith("看看这个 @.heng-agent/attachments/clipboard_img_3f2a.png 和 @.heng-agent/attachments/")
    assert out.endswith("，还有 @app.py") and len(attached) == 3
    # 不该动的：网址、不存在的路径、已经是 @ 的、普通文字里的斜杠
    keep = f"见 https://example.com/a/b.png 和 {temp / 'nope.png'} 以及 @app.py 还有 a/b 与 1/2"
    assert absorb_paths(keep, work) == (keep, [])


def test_zhima_style_screenshot_reaches_model_as_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fake_model import FakeModel

    work, temp = tmp_path / "proj", tmp_path / "Temp"
    work.mkdir()
    temp.mkdir()
    shot = temp / "clipboard_img_9c1d.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    model = FakeModel([["看到截图了"]])
    console = Console(record=True, width=120, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "home"), model), console=console)
    lines = iter([f"这个界面哪里不对 {shot}", "/exit"])  # 智码把截图路径写进终端后的样子
    monkeypatch.setattr(repl, "read", lambda: next(lines))
    repl.loop()
    sent = model.calls[0][-1]["content"]
    assert [b["type"] for b in sent] == ["text", "image"]  # 图片真的发给了模型
    assert "@.heng-agent/attachments/clipboard_img_9c1d.png" in sent[0]["text"]
    assert "已附上" in console.export_text()


def test_ctrl_v_binding(tmp_path: Path) -> None:
    box, pipe = _box(tmp_path, on_ctrl_v=lambda: ("@shot.png ", "已粘贴图片 shot.png"))
    pipe.send_text("图 \x16\r")  # Ctrl+V
    assert box.prompt() == "图 @shot.png "


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="只在 Windows 上调 PowerShell")
def test_powershell_receives_path_argument(tmp_path: Path) -> None:
    """真实调用 PowerShell：保存路径（含中文与空格）必须原样传进脚本。
    曾用 `-Command 脚本 参数` 传参——参数不会进 $args，路径为空，截图一直存不下来，而单测把剪贴板替换掉了没发现。"""
    from heng_agent.attachments import _powershell

    target = tmp_path / "中文 目录" / "截图 1.png"
    target.parent.mkdir()
    out, err = _powershell("Set-Content -Path $env:HENG_PASTE_TARGET -Value ok; 'done'", HENG_PASTE_TARGET=str(target))
    assert out == "done" and err == "" and target.read_text(encoding="utf-8-sig").strip() == "ok"
    _, err = _powershell("throw '故意出错'")
    assert err  # 出错时把错误交回来，而不是悄悄吞掉


def test_path_followed_by_chinese_punctuation(tmp_path: Path) -> None:
    """中文对话里路径后面常紧跟「，」「。」：它们不是空白，曾被连同后文一起当成路径而认不出来。"""
    from heng_agent.attachments import absorb_paths

    work, temp = tmp_path / "proj", tmp_path / "中文 目录"
    work.mkdir()
    temp.mkdir()
    shot = temp.parent / "截图.png"  # 路径本身含中文要能认
    shot.write_bytes(b"png")
    out, attached = absorb_paths(f"看看 {shot}，帮我分析一下。还有@别的.png", work)
    assert len(attached) == 1  # 含中文的路径认出来了，只认这一个（后面的 @别的.png 本来就是 @ 写法，不动）
    assert out.startswith("看看 @.heng-agent/attachments/") and "，帮我分析一下。还有@别的.png" in out

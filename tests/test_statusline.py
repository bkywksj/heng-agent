"""两行状态栏：窄窗口按重要程度去掉内容、权限模式单独一行；git 分支；
用户的状态栏命令（兼容 Claude Code 的 statusLine）。"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from rich.cells import cell_len

from heng_agent.core.config import Settings
from heng_agent.ui import statusline
from heng_agent.ui.prompt import toolbar


def _lines(ft: object) -> list[str]:
    return "".join(part[1] for part in ft).split("\n")  # type: ignore[attr-defined]


FULL = {"branch": "master", "dirty": True, "speed": 52.4, "mcp": ("class:bar.mcp", "  ✓ MCP 6")}


def test_wide_window_shows_everything() -> None:
    info, mode = _lines(toolbar("accept-edits", "yongye · claude-sonnet-5", r"E:\my\heng-agent", 46_000, 200_000,
                                width=200, **FULL))  # fmt: skip
    for piece in ("▣ heng-agent", "master*", "yongye · claude-sonnet-5", "✓ MCP 6", "52t/s", "██", "23% 46k/200k"):
        assert piece in info
    assert mode.startswith(" ⏵⏵ 自动改文件 已开启")


@pytest.mark.parametrize("width", [100, 80, 60, 50, 40])
def test_narrow_window_drops_by_priority_without_cutting(width: int) -> None:
    """以前一行全挤在一起，窄窗口里后半截直接被截掉（截图里「Shift+Tab 切换模式」只剩「Shi」）。"""
    info, mode = _lines(toolbar("ask", "yongye · claude-sonnet-5", r"E:\my\heng-agent", 46_000, 200_000,
                                width=width, **FULL))  # fmt: skip
    assert "▣ heng-agent" in info and "23%" in info and "手动确认 已开启" in mode  # 最要紧的始终在
    if cell_len(info) >= width:  # 都去掉了还放不下（极窄）：至少已经去到最少
        assert "52t/s" not in info and "master" not in info and "yongye" not in info and "░" not in info
    order = ["52t/s", "master", "yongye", "46k/200k", "██"]  # 去掉的顺序：前面的先去
    present = [p in info for p in order]
    assert present == sorted(present)  # 一旦某样被去掉，排在它前面的也都去掉了


def test_mode_line_per_mode_and_reminders() -> None:
    icons = {
        mode: _lines(toolbar(mode, "m", "w", 0, 1))[1].split()[0] for mode in ("ask", "accept-edits", "plan", "auto")
    }
    assert icons == {
        "ask": "⏵",
        "accept-edits": "⏵⏵",
        "plan": "❚❚",
        "auto": "⏵⏵⏵",
    }  # ⏵ 同 Claude Code；⏸ 在 Windows Terminal 里是 emoji 方块
    mode = _lines(toolbar("ask", "m", "w", 70_000, 100_000, background=2))[1]
    assert "后台任务 2" in mode and "距自动压缩 10%" in mode  # 提醒放第二行，不挤第一行


def test_icons_are_single_width() -> None:
    """emoji 在有的终端里算两格、有的算一格，对不齐；状态栏只用单宽字符。"""
    for ch in "▣|⏵❚█░✓✗✻":
        assert cell_len(ch) == 1, ch


def test_custom_line_replaces_first_line() -> None:
    info, mode = _lines(
        toolbar("plan", "m", "w", 0, 1, custom=[("", " 我的状态栏")], mcp=("class:bar.mcp", "  ✓ MCP 6"))
    )
    assert info == " 我的状态栏" and "计划模式 已开启" in mode  # 权限模式那行照留
    assert mode.endswith("· ✓ MCP 6")  # MCP 状态挪到第二行，不然看不到连接情况


# ---------- git 分支 ----------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)  # fmt: skip


def test_git_state(tmp_path: Path) -> None:
    assert statusline.git_state(tmp_path) is None  # 不是仓库
    _git(tmp_path, "init", "-b", "main")
    assert statusline.git_state(tmp_path) == ("main", False)  # 还没有提交
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-m", "x")
    assert statusline.git_state(tmp_path) == ("main", False)
    (tmp_path / "a.txt").write_text("2", encoding="utf-8")
    assert statusline.git_state(tmp_path) == ("main", True)  # 有没提交的改动


# ---------- 状态栏命令 ----------


def test_command_from_claude_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "node s.js"}}),
                                          encoding="utf-8")  # fmt: skip
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    home = tmp_path / "h"
    # 默认用 heng 自己的第一行：以前自动用 Claude Code 的，打开 heng 看到的是给 Claude 写的那一行（用户反馈）
    assert statusline.load_command(Settings(home=home)) is None
    assert statusline.load_command(Settings(home=home, status_line="claude")) == "node s.js"  # 明确要才用
    assert statusline.load_command(Settings(home=home, status_line="mine")) == "mine"
    assert statusline.load_command(Settings(home=home, status_line="claude", claude_user_config=False)) is None


def test_setting_accepts_claude_form() -> None:
    assert Settings(status_line={"type": "command", "command": "bash x.sh"}).status_line == "bash x.sh"
    with pytest.raises(ValueError):
        Settings(status_line={"type": "static"})


def test_project_config_cannot_set_command(tmp_path: Path) -> None:
    """状态栏命令会被执行：写在项目配置里，打开别人的仓库就会跑它指定的命令。只认用户配置。"""
    from heng_agent.configfile import settings_overrides

    home, work = tmp_path / "h", tmp_path / "w"
    (work / ".heng-agent").mkdir(parents=True)
    (work / ".heng-agent" / "config.json").write_text(json.dumps({"settings": {"status_line": "evil"}}),
                                                       encoding="utf-8")  # fmt: skip
    assert "status_line" not in settings_overrides(home, work)


def _script(tmp_path: Path, body: str) -> str:
    script = tmp_path / "bar.py"
    script.write_text(body, encoding="utf-8")
    return f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'


def test_run_command_gets_claude_payload_and_keeps_colors(tmp_path: Path) -> None:
    body = (
        "import json, sys\n"
        "d = json.load(sys.stdin)\n"
        "green, reset = chr(27) + '[32m', chr(27) + '[0m'\n"
        "print(green + d['model']['display_name'] + reset + ' ' + d['workspace']['current_dir'])\n"
        "print(d['context_window']['context_window_size'])\n"
    )
    command = _script(tmp_path, body)
    data = statusline.payload(session_id="s1", transcript="t", workdir=tmp_path, model="claude-sonnet-5",
                              provider="yongye", version="0", mode="ask", used=1000, window=200_000, total_in=1,
                              total_out=2, speed=None)  # fmt: skip
    out = statusline.run_command(command, data, tmp_path)
    assert isinstance(out, list)
    text = "".join(t for _, t in out)
    assert f"claude-sonnet-5 {tmp_path}" in text and "200000" in text
    green = "".join(t for style, t in out if "ansigreen" in style)  # 解析后一个字符一段
    assert green == "claude-sonnet-5"  # 脚本输出的颜色留着


def test_run_command_failures_are_explained(tmp_path: Path) -> None:
    silent = _script(tmp_path, "import sys\nsys.stderr.write('坏了\\n')\nsys.exit(3)\n")
    assert "坏了" in statusline.run_command(silent, {}, tmp_path)  # type: ignore[operator]


def test_background_refresh_does_not_stack() -> None:
    gate, calls, changed = threading.Event(), [], threading.Event()
    bg = statusline.Background(on_change=changed.set)

    def slow() -> str:
        calls.append(1)
        gate.wait(5)
        return "新"

    bg.refresh(slow)
    bg.refresh(slow)  # 上一次还没跑完：不叠着再起一个
    gate.set()
    assert changed.wait(5) and bg.value == "新" and calls == [1]


# ---------- 输出速度 ----------


class _Timed:
    """假模型：先「等」0.3 秒再一段段吐字，最后报 output_tokens；together=True 时模拟中转站攒齐了一次推过来。

    时间用假时钟（clock[0]，代替 agent 里的 time.monotonic）：以前真的 sleep，macOS 的 CI 机器上睡过了头，
    算出来的速度偏低、测试时过时不过（发 0.4.0 时撞到）。
    """

    def __init__(self, together: bool, clock: list[float]) -> None:
        self.together, self.clock = together, clock

    def stream(self, system: str, messages: list, tools: list):  # type: ignore[no-untyped-def]
        from heng_agent.core.types import TextDelta, TurnEnd

        self.clock[0] += 0.3  # 排队、读提示词
        for _ in range(5):
            if not self.together:
                self.clock[0] += 0.06
            yield TextDelta("字")
        yield TurnEnd("end_turn", {"input_tokens": 10, "output_tokens": 30})


@pytest.mark.parametrize("together", [False, True])
def test_output_speed(tmp_path: Path, together: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    from heng_agent import agent as agent_module
    from heng_agent.agent import Agent

    clock = [1000.0]
    monkeypatch.setattr(agent_module.time, "monotonic", lambda: clock[0])
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), _Timed(together, clock))  # type: ignore[arg-type]
    agent.run("你好", lambda e: None, lambda t, a: "once")
    if together:  # 一次推过来：按整个请求的用时（0.3 秒）算，不是除以几乎为 0 的时间
        assert agent.last_speed == pytest.approx(30 / 0.3)
    else:  # 一段段来：从第一段到结束（后 4 段，0.24 秒），不含前面 0.3 秒的等待
        assert agent.last_speed == pytest.approx(30 / 0.24)


@pytest.mark.parametrize(
    "workdir", [r"E:\my\heng-agent", "E:\\my\\heng-agent\\", "/home/u/heng-agent", "/home/u/heng-agent/"]
)
def test_project_name_with_either_separator(workdir: str) -> None:
    """项目名两种分隔符都认：以前用 Path(...).name，Linux 上不认反斜杠，Windows 写法的路径整串被当成名字（CI 撞到）。"""
    info = _lines(toolbar("ask", "m", workdir, 0, 1))[0]
    assert info.startswith(" ▣ heng-agent ")

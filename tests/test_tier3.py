"""第三档（第一批）：记忆管理、内置 /commit /review、新钩子事件、/doctor。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.commands import discover_commands, expand_command
from heng_agent.context import append_memory, nested_memory
from heng_agent.core.config import Settings
from heng_agent.core.types import Notice
from heng_agent.hooks import Hook
from heng_agent.ui.doctor import run_checks
from heng_agent.ui.repl import Repl

# ---------- 记忆 ----------


def test_append_memory(tmp_path: Path) -> None:
    path = tmp_path / "HENG.md"
    append_memory(path, "# 这个项目用 pnpm，不用 npm")
    append_memory(path, "#测试用 vitest")
    assert path.read_text(encoding="utf-8") == "# 记忆\n- 这个项目用 pnpm，不用 npm\n- 测试用 vitest\n"
    with pytest.raises(Exception, match="空"):
        append_memory(path, "#   ")


def test_nested_memory_once_and_outer_first(tmp_path: Path) -> None:
    work = tmp_path / "w"
    (work / "frontend" / "src").mkdir(parents=True)
    (work / "HENG.md").write_text("根目录（已在系统提示词里）", encoding="utf-8")
    (work / "frontend" / "HENG.md").write_text("前端：用 pnpm", encoding="utf-8")
    (work / "frontend" / "src" / "HENG.md").write_text("组件用函数式写法", encoding="utf-8")
    seen: set[str] = set()
    found = nested_memory(work / "frontend" / "src" / "App.tsx", work, seen)
    assert [text for _, text in found] == ["前端：用 pnpm", "组件用函数式写法"]  # 外层在前，根目录那份不重复附
    assert nested_memory(work / "frontend" / "src" / "b.ts", work, seen) == []  # 每份只附一次
    assert nested_memory(tmp_path / "外面.py", work, set()) == []  # 工作目录以外不管


def test_agent_attaches_subdir_memory_to_tool_result(tmp_path: Path) -> None:
    work = tmp_path / "w"
    (work / "api").mkdir(parents=True)
    (work / "api" / "HENG.md").write_text("接口统一返回 {code, data}", encoding="utf-8")
    (work / "api" / "user.py").write_text("x = 1\n", encoding="utf-8")
    model = FakeModel([[("tool", "read_file", {"path": "api/user.py"}), ("tool", "read_file", {"path": "api/user.py"})],
                       ["好"]])  # fmt: skip
    agent = Agent(Settings(workdir=work, home=tmp_path / "h"), model)
    agent.run("看看", lambda e: None, lambda t, a: "deny")
    results = model.calls[-1][-1]["content"]
    assert "接口统一返回" in results[0]["content"] and "接口统一返回" not in results[1]["content"]


def test_quick_memory_and_memory_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "w"
    work.mkdir()
    console = Console(record=True, width=140, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([])), console=console)
    monkeypatch.setattr(repl, "_ask", lambda prompt="": "p")  # 选择器用不了：退回打字，记到项目
    repl.quick_memory("# 提交信息用中文")
    assert "提交信息用中文" in (work / "AGENTS.md").read_text(encoding="utf-8")  # 项目还没有说明：新建 AGENTS.md
    assert "提交信息用中文" in repl.agent.system  # 立即生效
    repl.handle_command("/memory")
    shown = console.export_text()  # export_text 默认会清空已记录的内容：只取一次
    assert "常驻说明" in shown and "AGENTS.md" in shown and "写到哪里" in shown


# ---------- 内置提示词命令 ----------


def test_builtin_prompt_commands(tmp_path: Path) -> None:
    home, work = tmp_path / "h", tmp_path / "w"
    work.mkdir()
    commands = discover_commands(home, work)
    assert {"init", "commit", "review"} <= set(commands)
    init = expand_command("/init", commands)
    assert init is not None and "HENG.md" in init and "CLAUDE.md" in init and "不要整份重写" in init
    plain = expand_command("/commit", commands)
    assert plain is not None and "git diff" in plain and "不要 push" in plain and not plain.endswith("$ARGUMENTS")
    with_args = expand_command("/review 重点看并发", commands)
    assert with_args is not None and with_args.endswith("重点看并发")
    (work / ".heng-agent" / "commands").mkdir(parents=True)
    (work / ".heng-agent" / "commands" / "commit.md").write_text("按我们团队的规矩提交", encoding="utf-8")
    assert expand_command("/commit", discover_commands(home, work)) == "按我们团队的规矩提交"  # 项目同名的覆盖内置


# ---------- 新钩子事件 ----------


def _hook(tmp_path: Path, event: str, code: str) -> Hook:
    script = tmp_path / f"{event}.py"
    script.write_text(code, encoding="utf-8")
    return Hook(event, ".*", f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"')  # type: ignore[arg-type]


def test_user_prompt_submit_blocks_or_adds_context(tmp_path: Path) -> None:
    block = _hook(tmp_path, "user_prompt_submit", "import os, sys\n"
                  "if 'sk-' in os.environ['HENG_PROMPT']:\n    print('消息里像有密钥'); sys.exit(2)\n"
                  "print('当前分支：feature/login')\n")  # fmt: skip
    model = FakeModel([["好"]])
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), model, hooks=[block])
    events: list[Any] = []
    done = agent.run("我的密钥是 sk-123", events.append, lambda t, a: "deny")
    assert done.rounds == 0 and not model.calls and agent.messages == []  # 拦下了：没发给模型
    assert any(isinstance(e, Notice) and "像有密钥" in e.text for e in events)
    agent.run("看看登录页", lambda e: None, lambda t, a: "deny")
    assert "当前分支：feature/login" in model.calls[0][0]["content"][0]["text"]  # 输出作为补充上下文附上


def test_stop_hook_requires_continuing_at_most_three_times(tmp_path: Path) -> None:
    always_unhappy = _hook(tmp_path, "stop", "import sys\nprint('测试还没过，接着修'); sys.exit(2)\n")
    model = FakeModel([["一"], ["二"], ["三"], ["四"], ["五"]])
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), model, hooks=[always_unhappy])
    done = agent.run("修 bug", lambda e: None, lambda t, a: "deny")
    assert len(model.calls) == 4 and done.rounds == 4  # 原本 1 次 + 最多继续 3 次，不死循环
    continues = [m for m in agent.messages if m["role"] == "user" and "stop 钩子要求继续" in m["content"][0]["text"]]
    assert len(continues) == 3 and "测试还没过" in continues[0]["content"][0]["text"]


def test_session_start_hook_goes_into_system_prompt(tmp_path: Path) -> None:
    hook = _hook(tmp_path, "session_start", "print('今天的值班：张三')\n")
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([]), hooks=[hook])
    assert "今天的值班：张三" in agent.system
    agent.reload_system()
    assert "今天的值班：张三" in agent.system  # 重建系统提示词时保留


# ---------- /doctor ----------


def test_doctor(tmp_path: Path) -> None:
    from heng_agent.model.client import make_client
    from heng_agent.model.providers import Profile

    work = tmp_path / "w"
    work.mkdir()
    profile = Profile(protocol="anthropic", base_url="http://127.0.0.1:9", model="m", api_key="sk-x")  # 连不上的地址
    console = Console(record=True, width=160, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), make_client(profile, 10)), console=console)
    checks = {name: (status, note) for status, name, note in run_checks(repl)}
    assert checks["配置文件"][0] == "ok" and checks["密钥"][0] == "ok" and checks["会话目录"][0] == "ok"
    assert checks["服务商连接"][0] == "fail"  # 连不上要如实报出来
    repl.handle_command("/doctor")
    shown = console.export_text()
    assert "✗" in shown and "✓" in shown


# ---------- 工具过程的显示 ----------


def test_tool_label_and_approval_body() -> None:
    from heng_agent.mcp import MCPTool
    from heng_agent.tools.base import builtin_tools
    from heng_agent.ui.render import approval_body, tool_label

    assert tool_label("mcp__sigil__list_credentials") == "sigil · list_credentials"
    assert tool_label("bash") == "运行" and tool_label("custom_tool") == "custom_tool"

    class FakeClient:
        config = type("C", (), {"name": "sigil"})()

    tool = MCPTool(FakeClient(), {"name": "list_credentials", "description": "列出金库里的凭据\n更多说明",  # type: ignore[arg-type]
                                  "inputSchema": {"type": "object", "properties": {}}})  # fmt: skip
    body = approval_body(tool, {})
    assert "列出金库里的凭据" in body and "（无参数）" in body  # 没有参数也不再是空框
    nested = approval_body(tool, {"filter": {"platform": "github", "limit": 5}})
    assert '"platform": "github"' in nested  # MCP 的嵌套参数按 JSON 排版
    bash = {t.name: t for t in builtin_tools()}["bash"]
    assert approval_body(bash, {"command": "pytest -q"}) == "pytest -q"  # 内置命令只显示要跑什么


def test_tool_lines_are_compact(tmp_path: Path) -> None:
    from heng_agent.core.types import TextDelta, ToolFinished, ToolResult, ToolStarted

    console = Console(record=True, width=60, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([])), console=console)
    repl.on_event(TextDelta("我查一下"))
    repl.on_event(ToolStarted("t1", "bash", {"command": "echo " + "很长的参数" * 30}))
    repl.on_event(ToolFinished("t1", "bash", ToolResult("一行很长的输出" * 30 + "\n第二行")))
    repl.on_event(ToolStarted("t2", "read_file", {"path": "a.py"}))
    lines = console.export_text().splitlines()
    tool_lines = [line for line in lines if line.startswith(("●", "  ⎿", "    "))]
    assert all(len(line) <= 60 for line in tool_lines)  # 每条只占一行：超长截断，不折行
    assert any(line.startswith("● 运行 echo") and line.endswith("…") for line in lines)
    start = lines.index(next(line for line in lines if line.startswith("● 读取")))
    assert lines[start - 1].strip() == "" and lines[start - 2].strip() != ""  # 每次工具调用之间空一行（只空一行）


def test_banner_suggests_init_only_without_heng_md(tmp_path: Path) -> None:
    def banner_text(work: Path) -> str:
        console = Console(record=True, width=160, force_terminal=False)
        Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([])), console=console).banner()
        return console.export_text()

    work = tmp_path / "w"
    work.mkdir()
    assert "/init" in banner_text(work)  # 项目还没有说明：提醒一句
    (work / "HENG.md").write_text("# 说明\n", encoding="utf-8")
    assert "/init" not in banner_text(work)  # 有了就不再提


def test_blocks_are_separated_by_exactly_one_blank_line(tmp_path: Path) -> None:
    """提问、思考摘要、回复、每次工具调用、插话、过程提示、结尾那行：块与块之间空一行，不挤也不空两行。"""
    from heng_agent.core import types as t

    console = Console(record=True, width=80, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([])), console=console)
    repl.echo_user("看看项目")
    events = [t.ThinkingDelta("想"), t.TextDelta("先看目录。"), t.ToolStarted("t1", "glob", {"pattern": "*"}),
              t.ToolFinished("t1", "glob", t.ToolResult("a.py")),
              t.ToolStarted("t2", "bash", {"command": "echo ok"}), t.ToolFinished("t2", "bash", t.ToolResult("ok")),
              t.UserInterjection("顺便看 README"), t.Notice("正在压缩"), t.TextDelta("看完了。"),
              t.TurnDone({"input_tokens": 1, "output_tokens": 1}, rounds=2)]  # fmt: skip
    for event in events:
        repl.on_event(event)
    repl._unspin()
    lines = [line.strip() for line in console.export_text().splitlines()]
    starts = ("▌", "✻", "先看目录", "● 找文件", "● 运行", "⋯", "看完了")
    for i, line in enumerate(lines):
        if i and line.startswith(starts) and not lines[i - 1].startswith("▌"):
            assert lines[i - 1] == "", f"「{line}」前面没空行"
    assert all(lines[i] or lines[i + 1] for i in range(len(lines) - 1)), "出现了连续两行空行"

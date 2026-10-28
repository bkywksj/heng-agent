"""计划模式：模型读完代码用 exit_plan_mode 交计划 → 用户选按哪种模式执行或继续规划 → 批准后同一轮接着做。

以前计划模式只是「只读」：模型出完方案就停了，用户得自己切模式、再说一遍「按刚才的做」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import AgentEvent, Notice

PLAN = "## 计划\n1. 新建 hello.txt，写上 hi\n2. 读一遍确认"


def _agent(tmp_path: Path, script: list[Any], mode: str = "plan") -> tuple[Agent, FakeModel]:
    work = tmp_path / "w"
    work.mkdir(parents=True)
    model = FakeModel(script)
    return Agent(Settings(workdir=work, home=tmp_path / "h", permission_mode=mode), model), model  # type: ignore[arg-type]


class Answers:
    """确认对话：交计划时按 plan_answer 回答，其余（改文件、跑命令）按 tool_answer；记下被问到的工具。"""

    def __init__(self, plan_answer: str, tool_answer: str = "once") -> None:
        self.plan_answer, self.tool_answer = plan_answer, tool_answer
        self.asked: list[str] = []
        self.events: list[AgentEvent] = []

    def approve(self, tool: Any, args: dict[str, Any]) -> str:
        self.asked.append(tool.name)
        return self.plan_answer if tool.name == "exit_plan_mode" else self.tool_answer


def _script() -> list[Any]:
    return [
        [("tool", "exit_plan_mode", {"plan": PLAN})],
        [("tool", "write_file", {"path": "hello.txt", "content": "hi\n"})],  # 批准后同一轮接着做
        ["做完了"],
    ]


def _tool_result(model: FakeModel, call: int) -> str:
    return str(model.calls[call][-1]["content"][0]["content"])


def test_tool_only_offered_in_plan_mode(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path, [])
    assert "exit_plan_mode" in [s["name"] for s in agent.tool_specs()]
    agent.permissions.mode = "ask"
    assert "exit_plan_mode" not in [s["name"] for s in agent.tool_specs()]  # 别的模式里给了，模型会拿它来请示


def test_system_prompt_follows_current_mode(tmp_path: Path) -> None:
    """以前权限模式写死在启动时生成的提示词里：中途切了模式，模型还以为是原来的。"""
    agent, _ = _agent(tmp_path, [])
    assert "当前：plan" in agent.system_prompt() and "exit_plan_mode" in agent.system_prompt()
    agent.permissions.mode = "accept-edits"
    assert "当前：accept-edits" in agent.system_prompt() and "exit_plan_mode" not in agent.system_prompt()


@pytest.mark.parametrize(
    ("answer", "mode", "asked_for_write"), [("once", "ask", True), ("always", "accept-edits", False)]
)
def test_approved_plan_switches_mode_and_continues(
    tmp_path: Path, answer: str, mode: str, asked_for_write: bool
) -> None:
    agent, model = _agent(tmp_path, _script())
    answers = Answers(answer)
    agent.run("给项目加个 hello.txt", answers.events.append, answers.approve)
    assert agent.permissions.mode == mode
    assert (agent.settings.workdir / "hello.txt").read_text(encoding="utf-8") == "hi\n"  # 同一轮里接着按计划做了
    assert ("write_file" in answers.asked) is asked_for_write  # ask 改文件前问；accept-edits 不问
    assert "用户批准了计划" in _tool_result(model, 1) and mode in _tool_result(model, 1)
    assert any(isinstance(e, Notice) and "计划已批准" in e.text for e in answers.events)
    # 批准后的下一轮：模型看到的是新模式，交计划的工具也不再给了
    assert "当前：plan" in model.seen_systems[0] and f"当前：{mode}" in model.seen_systems[1]
    assert "exit_plan_mode" in [t["name"] for t in model.seen_specs[0]]
    assert "exit_plan_mode" not in [t["name"] for t in model.seen_specs[1]]


def test_rejected_plan_stays_in_plan_mode(tmp_path: Path) -> None:
    agent, model = _agent(tmp_path, [[("tool", "exit_plan_mode", {"plan": PLAN})], ["好的，你说要怎么改"]])
    answers = Answers("deny")
    agent.run("给项目加个 hello.txt", answers.events.append, answers.approve)
    assert agent.permissions.mode == "plan"
    result = _tool_result(model, 1)
    assert "没有批准" in result and "停下" in result
    assert not (agent.settings.workdir / "hello.txt").exists()


def test_plan_tool_outside_plan_mode_or_empty(tmp_path: Path) -> None:
    agent, model = _agent(tmp_path, [[("tool", "exit_plan_mode", {"plan": PLAN})], ["好"]], mode="ask")
    answers = Answers("once")
    agent.run("做吧", answers.events.append, answers.approve)
    assert "不是计划模式" in _tool_result(model, 1) and "exit_plan_mode" not in answers.asked  # 不弹确认
    agent2, model2 = _agent(tmp_path / "b", [[("tool", "exit_plan_mode", {"plan": "  "})], ["好"]])
    agent2.run("做吧", answers.events.append, answers.approve)
    assert "plan 是空的" in _tool_result(model2, 1) and agent2.permissions.mode == "plan"


def test_subagents_never_get_the_plan_tool(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path, [])
    sub, _ = agent._spawn_subagent()
    assert "exit_plan_mode" not in sub.tools


# ---------- 界面：完整显示计划，三个选项 ----------


def _repl(tmp_path: Path) -> tuple[Any, Console]:
    from heng_agent.ui.repl import Repl

    agent, _ = _agent(tmp_path, [])
    console = Console(record=True, width=120, force_terminal=False)
    return Repl(agent, console=console), console


def test_repl_shows_plan_and_maps_typed_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repl, console = _repl(tmp_path)
    tool = repl.agent.tools["exit_plan_mode"]
    for typed, expected in (("a", "always"), ("", "once"), ("n", "deny")):  # 测试里没有终端：退回打字
        monkeypatch.setattr(repl.console, "input", lambda prompt="", t=typed: t)
        assert repl.approve(tool, {"plan": PLAN}) == expected
    shown = console.export_text()
    assert "计划" in shown and "新建 hello.txt" in shown  # Markdown 渲染出来的计划正文
    assert "继续规划" in shown


def test_repl_esc_means_keep_planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repl, console = _repl(tmp_path)
    monkeypatch.setattr(repl, "_choose", lambda title, options: None)  # 选择器里按 ESC
    assert repl.approve(repl.agent.tools["exit_plan_mode"], {"plan": PLAN}) == "deny"
    assert "继续规划" in console.export_text()


# ---------- ACP：确认框里看得到计划，选项写成「开始执行」 ----------


def test_acp_preview_and_kind() -> None:
    from heng_agent.acp_server import TOOL_KIND, tool_preview

    assert TOOL_KIND["exit_plan_mode"][0] == "switch_mode"
    preview = tool_preview("exit_plan_mode", {"plan": PLAN})
    assert preview is not None and "新建 hello.txt" in str(preview)

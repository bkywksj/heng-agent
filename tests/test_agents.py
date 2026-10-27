"""自定义子智能体角色：.heng-agent/agents/*.md 的解析、按角色派子智能体、带改动工具的确认与撤销、/agents。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.agents import discover_agents
from heng_agent.core.config import Settings
from heng_agent.ui import repl as repl_module
from heng_agent.ui.repl import Repl


def _write(folder: Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text, encoding="utf-8")


def test_discover_agents(tmp_path: Path) -> None:
    home, work = tmp_path / "h", tmp_path / "w"
    _write(home / "agents", "审查员", "---\ndescription: 用户级审查员\n---\n用户版")
    _write(work / ".heng-agent" / "agents", "审查员", "---\ndescription: 项目级审查员\n---\n项目版")
    _write(
        work / ".heng-agent" / "agents",
        "测试员",
        "---\ndescription: 跑测试\ntools: read_file, bash，task, 乱写\n---\n先跑测试",
    )
    _write(work / ".heng-agent" / "agents", "调研员", "只读，没写 frontmatter")
    agents = discover_agents(home, work)
    assert agents["审查员"].description == "项目级审查员" and agents["审查员"].prompt == "项目版"  # 项目覆盖用户
    tester = agents["测试员"]
    assert tester.tools == ("read_file", "bash") and tester.unknown_tools == ("task", "乱写")  # 不许套娃、不认识的忽略
    assert not tester.read_only
    assert agents["调研员"].tools == ("read_file", "glob", "grep") and agents["调研员"].read_only  # 不写 tools 就是只读


def _agent(tmp_path: Path, script: list[Any], mode: str = "ask") -> tuple[Agent, FakeModel]:
    work = tmp_path / "w"
    _write(work / ".heng-agent" / "agents", "调研员", "---\ndescription: 只读调研\n---\n你是调研员，只汇报文件内容。")
    _write(
        work / ".heng-agent" / "agents",
        "修复员",
        "---\ndescription: 改代码\ntools: read_file, edit_file\n---\n你是修复员。",
    )
    (work / "a.py").write_text("x = 1\n", encoding="utf-8")
    model = FakeModel(script)
    return Agent(Settings(workdir=work, home=tmp_path / "h", permission_mode=mode), model), model  # type: ignore[arg-type]


def test_read_only_role_gets_its_prompt(tmp_path: Path) -> None:
    agent, model = _agent(tmp_path, [
        [("tool", "task", {"description": "看文件", "prompt": "a.py 里是什么", "agent": "调研员"})],
        [("tool", "read_file", {"path": "a.py"})],  # 子智能体
        ["x 等于 1"],  # 子智能体的结论
        ["子智能体说 x 等于 1"],  # 主智能体
    ])  # fmt: skip
    agent.run("派调研员看看", lambda e: None, lambda t, a: "deny")
    sub_first = model.calls[1][0]["content"][0]["text"]
    assert sub_first.startswith("你是调研员") and "a.py 里是什么" in sub_first
    assert "调研员：只读调研" in agent.tools["task"].description and "修复员" in agent.tools["task"].description
    result = model.calls[3][-1]["content"][0]["content"]
    assert "x 等于 1" in result


def test_editing_role_asks_and_can_be_undone(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path, [
        [("tool", "task", {"description": "修", "prompt": "把 x 改成 2", "agent": "修复员"})],
        [("tool", "edit_file", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"})],  # 子智能体
        ["改好了"],
        ["修复员改好了"],
    ])  # fmt: skip
    asked: list[str] = []

    def approve(tool: Any, args: dict[str, Any]) -> str:
        asked.append(tool.name)
        return "once"

    agent.run("派修复员改一下", lambda e: None, approve)
    path = agent.settings.workdir / "a.py"
    assert asked == ["edit_file"]  # 子智能体改文件前通过主智能体的确认对话问了用户
    assert path.read_text(encoding="utf-8") == "x = 2\n"
    agent.undo()
    assert path.read_text(encoding="utf-8") == "x = 1\n"  # 子智能体改的也记进了主智能体这一问，能撤销


def test_editing_roles_never_run_in_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import heng_agent.agent as agent_module

    class NoPool:
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise AssertionError("带改动工具的角色不应并行")

    agent, _ = _agent(tmp_path, [
        [("tool", "task", {"description": "一", "prompt": "看", "agent": "修复员"}),
         ("tool", "task", {"description": "二", "prompt": "看", "agent": "调研员"})],
        ["一的结论"], ["二的结论"], ["都好了"],
    ])  # fmt: skip
    monkeypatch.setattr(agent_module, "ThreadPoolExecutor", NoPool)
    agent.run("派两个", lambda e: None, lambda t, a: "deny")  # 没抛：按顺序一个个跑的


def test_unknown_role(tmp_path: Path) -> None:
    agent, model = _agent(
        tmp_path, [[("tool", "task", {"description": "?", "prompt": "?", "agent": "不存在"})], ["好"]]
    )
    agent.run("派一个不存在的", lambda e: None, lambda t, a: "deny")
    result = model.calls[1][-1]["content"][0]
    assert result["is_error"] and "没有叫 不存在 的角色" in result["content"]


def test_agents_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "w"
    work.mkdir()
    console = Console(record=True, width=140, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([])), console=console)
    repl.handle_command("/agents")
    edited = "---\ndescription: 写单元测试\ntools: read_file, write_file\n---\n你是测试员。"
    monkeypatch.setattr(repl_module, "edit_in_editor", lambda text: edited)
    repl.handle_command("/agents new 测试员")
    assert (
        (work / ".heng-agent" / "agents" / "测试员.md")
        .read_text(encoding="utf-8")
        .startswith("---\ndescription: 写单元测试")
    )
    assert "测试员：写单元测试" in repl.agent.tools["task"].description  # 存好马上生效，模型能挑到
    repl.handle_command("/agents")
    shown = console.export_text()
    assert "没有自定义角色" in shown and "测试员" in shown and "会改动" in shown

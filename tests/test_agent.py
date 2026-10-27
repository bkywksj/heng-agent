"""对话循环与权限：用假模型按脚本对话，检查文件真实变化、喂回给模型的内容、确认对话有没有被调用。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_model import FakeModel
from heng_agent.agent import DENIED, INTERRUPTED, Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import AgentEvent, TextDelta, ToolFinished, TurnDone
from heng_agent.session import SessionStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    work = tmp_path / "proj"
    work.mkdir()
    return Settings(workdir=work, home=tmp_path / "home", max_rounds=5)


class Recorder:
    """收集事件，并按预设回答确认对话（记录被问了几次）。"""

    def __init__(self, answer: str = "once") -> None:
        self.events: list[AgentEvent] = []
        self.asked: list[str] = []
        self.answer = answer

    def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)

    def approve(self, tool: Any, args: dict[str, Any]) -> Any:
        self.asked.append(tool.name)
        return self.answer

    @property
    def text(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, TextDelta))

    @property
    def results(self) -> list[ToolFinished]:
        return [e for e in self.events if isinstance(e, ToolFinished)]


def _last_tool_results(model: FakeModel) -> list[dict[str, Any]]:
    return model.calls[-1][-1]["content"]


def test_plain_answer_is_one_round(settings: Settings) -> None:
    model = FakeModel([["你好，", "我是 heng-agent。"]])
    rec = Recorder()
    done = Agent(settings, model).run("你好", rec.on_event, rec.approve)
    assert done.rounds == 1 and rec.text == "你好，我是 heng-agent。"
    assert isinstance(rec.events[-1], TurnDone) and done.usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_tool_result_is_fed_back_to_model(settings: Settings) -> None:
    (settings.workdir / "a.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    model = FakeModel([["先看看文件", ("tool", "read_file", {"path": "a.txt"})], ["文件有两行。"]])
    rec = Recorder()
    Agent(settings, model).run("a.txt 有几行？", rec.on_event, rec.approve)
    fed = _last_tool_results(model)
    assert fed[0]["type"] == "tool_result" and "1\t第一行" in fed[0]["content"] and not fed[0]["is_error"]
    assert rec.asked == []  # 读文件不需要确认
    assert model.seen_tools == [
        "read_file",
        "glob",
        "grep",
        "write_file",
        "edit_file",
        "bash",
        "bash_output",
        "kill_task",
        "todo",
        "web_fetch",
        "task",
    ]


def test_edit_runs_only_after_approval(settings: Settings) -> None:
    target = settings.workdir / "app.py"
    target.write_text("x = 1\n", encoding="utf-8")
    script = [[("tool", "edit_file", {"path": "app.py", "old_string": "x = 1", "new_string": "x = 2"})], ["改好了"]]
    rec = Recorder("once")
    Agent(settings, FakeModel(list(script))).run("把 x 改成 2", rec.on_event, rec.approve)
    assert target.read_text(encoding="utf-8") == "x = 2\n" and rec.asked == ["edit_file"]


def test_denied_tool_does_nothing_and_model_is_told(settings: Settings) -> None:
    target = settings.workdir / "app.py"
    target.write_text("x = 1\n", encoding="utf-8")
    model = FakeModel([[("tool", "write_file", {"path": "app.py", "content": "boom"})], ["好的，不改了"]])
    rec = Recorder("deny")
    Agent(settings, model).run("重写它", rec.on_event, rec.approve)
    assert target.read_text(encoding="utf-8") == "x = 1\n"  # 拒绝时文件不变
    assert rec.results[0].denied and _last_tool_results(model)[0]["content"] == DENIED


def test_plan_mode_refuses_without_asking(settings: Settings) -> None:
    settings = settings.model_copy(update={"permission_mode": "plan"})
    model = FakeModel([[("tool", "bash", {"command": "echo hi"})], ["那我只做分析"]])
    rec = Recorder("once")
    Agent(settings, model).run("跑一下", rec.on_event, rec.approve)
    assert rec.asked == [] and rec.results[0].denied


def test_accept_edits_mode_and_always_allow(settings: Settings) -> None:
    settings = settings.model_copy(update={"permission_mode": "accept-edits"})
    model = FakeModel(
        [
            [("tool", "write_file", {"path": "n.txt", "content": "hi"}), ("tool", "bash", {"command": "echo 1"})],
            [("tool", "bash", {"command": "echo 2"})],
            ["完成"],
        ]
    )
    rec = Recorder("always")
    Agent(settings, model).run("做吧", rec.on_event, rec.approve)
    assert (settings.workdir / "n.txt").read_text(encoding="utf-8") == "hi"
    assert rec.asked == ["bash"]  # 写文件不问；bash 选了「一直允许」后第二次不再问


def test_unknown_tool_and_bad_args_become_errors_not_crashes(settings: Settings) -> None:
    model = FakeModel([[("tool", "nope", {}), ("tool", "read_file", {})], ["抱歉"]])
    rec = Recorder()
    Agent(settings, model).run("试试", rec.on_event, rec.approve)
    fed = _last_tool_results(model)
    assert fed[0]["is_error"] and "没有叫 nope 的工具" in fed[0]["content"]
    assert fed[1]["is_error"] and "参数有误" in fed[1]["content"]


def test_path_escape_is_refused(settings: Settings) -> None:
    outside = settings.workdir.parent / "secret.txt"
    model = FakeModel([[("tool", "write_file", {"path": "../secret.txt", "content": "x"})], ["好"]])
    rec = Recorder("once")
    Agent(settings, model).run("写到外面", rec.on_event, rec.approve)
    assert not outside.exists() and "工作目录" in _last_tool_results(model)[0]["content"]


def test_max_rounds_stops_runaway_loops(settings: Settings) -> None:
    model = FakeModel([[("tool", "glob", {"pattern": "*"})]] * 5)
    rec = Recorder()
    done = Agent(settings, model).run("一直找", rec.on_event, rec.approve)
    assert done.rounds == 5 and "达到上限" in rec.text


def test_interrupted_tool_call_is_repaired_before_next_question(settings: Settings) -> None:
    agent = Agent(settings, FakeModel([["好的"]]))
    agent.messages = [
        {"role": "user", "content": [{"type": "text", "text": "跑测试"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}]},
    ]
    rec = Recorder()
    agent.run("算了", rec.on_event, rec.approve)
    repaired = agent.messages[2]["content"][0]
    assert repaired["tool_use_id"] == "t1" and repaired["content"] == INTERRUPTED


def test_session_is_saved_and_resumable(settings: Settings) -> None:
    store = SessionStore(settings.home, settings.workdir)
    agent = Agent(settings, FakeModel([["记住了：项目叫衡"]]), session=store)
    rec = Recorder()
    agent.run("记住项目名", rec.on_event, rec.approve)
    latest = SessionStore.latest(settings.home, settings.workdir)
    assert latest is not None and latest.id == store.id
    resumed = Agent(settings, FakeModel([["你说过项目叫衡"]]))
    assert resumed.resume(latest) == 2 and resumed.usage["input_tokens"] == 10
    resumed.run("项目叫什么？", rec.on_event, rec.approve)
    assert len(resumed.messages) == 4


def test_project_memory_goes_into_system_prompt(settings: Settings) -> None:
    (settings.workdir / "HENG.md").write_text("提交信息一律中文。", encoding="utf-8")
    agent = Agent(settings, FakeModel([]))
    assert "项目说明 HENG.md" in agent.system and "提交信息一律中文。" in agent.system


def test_auto_mode_runs_everything_without_asking(settings: Settings) -> None:
    settings = settings.model_copy(update={"permission_mode": "auto"})
    model = FakeModel(
        [
            [("tool", "write_file", {"path": "a.txt", "content": "x"}), ("tool", "bash", {"command": "echo ok"})],
            ["完成"],
        ]
    )
    rec = Recorder("deny")  # 即使确认对话会拒绝，auto 模式也根本不问
    Agent(settings, model).run("做", rec.on_event, rec.approve)
    assert rec.asked == [] and (settings.workdir / "a.txt").exists()
    assert not any(r.denied for r in rec.results)

"""上下文压缩：触发时机、压缩后历史的形状（user / assistant 交替、工具调用与结果成对）、摘要输入的裁剪。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.compaction import split_tail, transcript
from heng_agent.core.config import Settings
from heng_agent.core.types import AgentEvent, Notice


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    work = tmp_path / "proj"
    work.mkdir()
    # 假模型每次报 10 个输入 token；上下文设成 10、阈值 0.8：上一次请求之后就该压缩
    return Settings(workdir=work, home=tmp_path / "home", context_window=10, compact_ratio=0.8)


def _text(msg: dict) -> str:
    return "".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text")


def test_compacts_between_questions(settings: Settings) -> None:
    model = FakeModel([["答1"], ["摘要：用户问过问题1，已回答。"], ["答2"]])
    agent = Agent(settings, model)
    events: list[AgentEvent] = []
    agent.run("问题1", events.append, lambda t, a: "deny")
    agent.run("问题2", events.append, lambda t, a: "deny")
    summary_request = _text(model.calls[1][0])
    assert "问题1" in summary_request and "答1" in summary_request  # 旧问答交给模型总结
    roles = [m["role"] for m in agent.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert "工作摘要" in _text(agent.messages[0]) and "问题1，已回答" in _text(agent.messages[0])
    assert _text(agent.messages[2]) == "问题2" and _text(agent.messages[3]) == "答2"
    notices = [e.text for e in events if isinstance(e, Notice)]
    assert any("正在压缩" in n for n in notices) and any("已压缩" in n for n in notices)


def test_compaction_mid_tool_loop_keeps_call_and_result_together(settings: Settings) -> None:
    model = FakeModel([[("tool", "glob", {"pattern": "*.py"})], ["摘要：在找 py 文件"], ["找完了"]])
    agent = Agent(settings, model)
    agent.run("找 py 文件", lambda e: None, lambda t, a: "deny")
    after = model.calls[-1]  # 压缩后真正发给模型的历史
    assert after[0]["role"] == "user" and "工作摘要" in _text(after[0])
    call, result = after[1], after[2]
    assert call["role"] == "assistant" and call["content"][0]["type"] == "tool_use"
    assert result["content"][0]["tool_use_id"] == call["content"][0]["id"]  # 成对保留，没被压掉一半


def test_nothing_to_compact_for_a_single_message(settings: Settings) -> None:
    agent = Agent(settings, FakeModel([]))
    agent.messages = [{"role": "user", "content": [{"type": "text", "text": "只有一句"}]}]
    events: list[AgentEvent] = []
    assert agent.compact(events.append, force=True) is False
    assert any("没有可压缩" in e.text for e in events if isinstance(e, Notice))


def test_below_threshold_does_not_compact(settings: Settings) -> None:
    settings = settings.model_copy(update={"context_window": 1000})
    model = FakeModel([["答1"], ["答2"]])  # 没有给摘要准备脚本：要是误触发压缩，假模型会报脚本不够
    agent = Agent(settings, model)
    agent.run("问题1", lambda e: None, lambda t, a: "deny")
    agent.run("问题2", lambda e: None, lambda t, a: "deny")
    assert len(agent.messages) == 4


def test_transcript_clips_tool_output_and_drops_oldest() -> None:
    history = [
        {"role": "user", "content": [{"type": "text", "text": "最早的问题"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t", "name": "bash", "input": {"command": "pytest"}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x" * 5000, "is_error": True}],
        },
    ]
    text = transcript(history, max_chars=100_000)
    assert "【助手调用工具 bash】" in text and "【工具结果（出错）】" in text and "截掉" in text
    assert "最早的问题" not in transcript(history, max_chars=200)  # 太长时丢最早的部分
    assert split_tail(history) == 1  # 末尾是工具结果：从带工具调用的助手消息开始保留

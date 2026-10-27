"""上下文一下子超出模型上限时的补救：识别各家「超长」报错 → 强制压缩重发 → 还超就截短工具结果 → 再不行照常报错。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from heng_agent.agent import SHRINK_TOOL_RESULT, Agent
from heng_agent.core.config import Settings
from heng_agent.core.errors import ModelError
from heng_agent.core.types import Message, ModelEvent, Notice, TextDelta, TurnEnd
from heng_agent.ui.prompt import toolbar

OVERFLOW = "模型服务返回 400：prompt is too long: 250000 tokens > 200000 maximum"


@pytest.mark.parametrize("message", [
    OVERFLOW,
    "模型服务返回 400：{'error': {'code': 'context_length_exceeded'}}",
    "This model's maximum context length is 65536 tokens. However, you requested 70000 tokens",
    "Invalid request: exceeded model token limit: 131072",
    "Range of input length should be [1, 30720]",
    "输入的上下文长度超过模型上限",
])  # fmt: skip
def test_overflow_detected(message: str) -> None:
    assert ModelError(message).overflow


def test_rate_limit_is_not_overflow() -> None:
    limited = ModelError("模型服务返回 429：Rate limit reached: tokens per min (TPM) limit 30000", retryable=True)
    assert not limited.overflow  # 限流可重试：不当成超长去压缩
    assert not ModelError("模型服务返回 401：invalid api key").overflow


class ScriptedClient:
    """按脚本回应每次请求：'overflow' = 报上下文超长；字符串 = 正常回答（压缩时做摘要也走这里）。"""

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.requests: list[list[Message]] = []

    def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> Iterator[ModelEvent]:
        self.requests.append([dict(m) for m in messages])
        step = self.script.pop(0)
        if step == "overflow":
            raise ModelError(OVERFLOW)
        yield TextDelta(step)
        yield TurnEnd("end_turn", {"input_tokens": 100, "output_tokens": 10})


def _history(turns: int, tool_result: str = "") -> list[Message]:
    messages: list[Message] = []
    for i in range(turns):
        messages.append({"role": "user", "content": [{"type": "text", "text": f"第 {i} 个问题"}]})
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"第 {i} 个回答"}]})
    if tool_result:
        messages.append({"role": "user", "content": [{"type": "text", "text": "读大文件"}]})
        messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file",
                                                          "input": {"path": "big.log"}}]})  # fmt: skip
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                     "content": tool_result}]})  # fmt: skip
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "读完了"}]})
    return messages


def _agent(tmp_path: Path, client: ScriptedClient, history: list[Message]) -> Agent:
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), client)  # type: ignore[arg-type]
    agent.messages = history
    return agent


def test_overflow_compacts_and_retries(tmp_path: Path) -> None:
    client = ScriptedClient(["overflow", "这是摘要：之前聊了 10 个问题", "答案是 42"])
    agent = _agent(tmp_path, client, _history(10))
    notices: list[str] = []
    done = agent.run("新问题", lambda e: notices.append(e.text) if isinstance(e, Notice) else None,
                     lambda t, a: "once")  # fmt: skip
    assert done.rounds == 1  # 补救的那次不算轮数
    assert agent.messages[-1]["content"][0]["text"] == "答案是 42"
    assert any("超出了模型的上限" in n for n in notices) and any("已压缩" in n for n in notices)
    assert len(client.requests[2]) < len(client.requests[0])  # 重发的是压缩后的短历史


def test_short_history_shrinks_big_tool_result(tmp_path: Path) -> None:
    big = "日志行\n" * 20_000  # 一个超大的工具结果：压缩会原样保留末尾，压了也没用
    client = ScriptedClient(["overflow", "overflow", "看完了"])
    agent = _agent(tmp_path, client, _history(0, tool_result=big))
    agent.run("总结一下日志", lambda e: None, lambda t, a: "once")
    result = agent.messages[2]["content"][0]["content"]
    assert len(result) < SHRINK_TOOL_RESULT + 100 and "这段工具结果被截短了" in result
    assert agent.messages[-1]["content"][0]["text"] == "看完了"


def test_gives_up_when_nothing_helps(tmp_path: Path) -> None:
    # 依次：正式请求超长 → 压缩时做摘要的请求也超长 → 改截短工具结果、重发仍超长 → 再截已经没得截 → 照常报错
    client = ScriptedClient(["overflow", "overflow", "overflow", "不该走到这里"])
    agent = _agent(tmp_path, client, _history(0, tool_result="x" * (SHRINK_TOOL_RESULT * 3)))
    notices: list[str] = []
    with pytest.raises(ModelError, match="prompt is too long"):
        agent.run("再试试", lambda e: notices.append(e.text) if isinstance(e, Notice) else None,
                  lambda t, a: "once")  # fmt: skip
    assert client.script == ["不该走到这里"]  # 没有一直重发
    assert sum("截短了" in n for n in notices) == 1  # 截过的不再截一遍、假装又有进展


def test_toolbar_shows_distance_to_compaction() -> None:
    def text(used: int) -> str:
        return "".join(part for _, part in toolbar("ask", "m", "/w", used, 100_000, compact_ratio=0.8))

    assert "自动压缩" not in text(30_000)
    assert "距自动压缩 10%" in text(70_000)
    assert "下一问前自动压缩" in text(85_000)


# ---------- 空回复：自动重新请求一次 ----------


def test_empty_reply_retried_once(tmp_path: Path) -> None:
    from fake_model import FakeModel
    from heng_agent.core.config import Settings as S

    model = FakeModel([[("think", "")], ["包邮门槛是 99 元"]])  # 第一次只有一个空思考块（实测中转站这样过）
    agent = Agent(S(workdir=tmp_path, home=tmp_path / "h"), model)
    notices: list[str] = []
    done = agent.run("包邮门槛多少", lambda e: notices.append(e.text) if isinstance(e, Notice) else None,
                     lambda t, a: "once")  # fmt: skip
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]  # 空回复不进历史
    assert agent.messages[-1]["content"][-1]["text"] == "包邮门槛是 99 元" and done.rounds == 1
    assert any("空回复" in n for n in notices) and done.usage["output_tokens"] == 10  # 两次请求的用量都记上


def test_empty_reply_twice_gives_up(tmp_path: Path) -> None:
    from fake_model import FakeModel
    from heng_agent.core.config import Settings as S

    model = FakeModel([[""], [""]])
    agent = Agent(S(workdir=tmp_path, home=tmp_path / "h"), model)
    agent.run("问", lambda e: None, lambda t, a: "once")
    assert len(model.calls) == 2 and agent.messages[-1]["content"][-1]["text"] == "（空回复）"  # 只重试一次

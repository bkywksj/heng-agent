"""思考模式：Anthropic 思考块（带签名）的解析、存史、原样传回、关闭后剥掉；OpenAI 的 reasoning 只显示；界面折叠。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import TextDelta, ThinkingBlock, ThinkingDelta, ToolCall, TurnDone
from heng_agent.model.client import make_client, parse_openai_sse, parse_sse, strip_thinking
from heng_agent.model.providers import Profile
from heng_agent.ui.repl import Repl


def _sse(events: list[dict[str, Any]]) -> list[str]:
    return [f"data: {json.dumps(e, ensure_ascii=False)}" for e in events]


def test_parse_thinking_with_signature() -> None:
    events = list(parse_sse(iter(_sse([
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "先看看"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "购物车"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "EqQB"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "xyz=="}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "redacted_thinking", "data": "密文"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "t1", "name": "read_file"}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    ]))))  # fmt: skip
    assert [e.text for e in events if isinstance(e, ThinkingDelta)] == ["先看看", "购物车"]
    blocks = [e.block for e in events if isinstance(e, ThinkingBlock)]
    assert blocks == [
        {"type": "thinking", "thinking": "先看看购物车", "signature": "EqQBxyz=="},  # 签名分片拼完整
        {"type": "redacted_thinking", "data": "密文"},  # 加密的思考原样留着
    ]
    assert any(isinstance(e, ToolCall) for e in events)


def test_request_body_on_and_off() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"type": "message_stop"}\n\n')

    history = [
        {"role": "user", "content": [{"type": "text", "text": "修 bug"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "想", "signature": "s"},
                                          {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]},
    ]  # fmt: skip
    client = make_client(Profile(protocol="anthropic", base_url="http://x", model="m"), 4096,
                         transport=httpx.MockTransport(handler))  # fmt: skip
    client.thinking_budget = 8000
    list(client.stream("", history, []))
    client.thinking_budget = 0
    list(client.stream("", history, []))
    on, off = bodies
    assert on["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert on["max_tokens"] == 8000 + 4096  # 必须大于思考预算，否则服务拒收
    assert on["messages"][1]["content"][0] == {"type": "thinking", "thinking": "想", "signature": "s"}  # 原样传回
    assert "thinking" not in off and off["max_tokens"] == 4096
    assert [b["type"] for b in off["messages"][1]["content"]] == ["tool_use"]  # 关了就剥掉
    assert history[1]["content"][0]["type"] == "thinking"  # 对话历史本身没被改


def test_strip_keeps_assistant_non_empty() -> None:
    only_thought = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "嗯", "signature": "s"}]}]
    assert strip_thinking(only_thought)[0]["content"] == [{"type": "text", "text": "（思考）"}]


def test_agent_stores_thinking_first_and_sets_budget(tmp_path: Path) -> None:
    model = FakeModel([[("think", "先读文件"), "我看看", ("tool", "read_file", {"path": "a.txt"})], ["好了"]])
    (tmp_path / "a.txt").write_text("内容", encoding="utf-8")
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h", thinking=True, thinking_budget=5000), model)
    seen: list[Any] = []
    agent.run("看下 a.txt", seen.append, lambda t, a: "once")
    assistant = agent.messages[1]["content"]
    assert [b["type"] for b in assistant] == ["thinking", "text", "tool_use"]  # 思考块在最前面
    assert assistant[0]["signature"] == "sig-1"
    assert model.calls[1][1]["content"][0]["type"] == "thinking"  # 调工具后的下一次请求原样带回
    assert model.seen_budgets == [5000, 5000]
    assert any(isinstance(e, ThinkingDelta) for e in seen)  # 思考文字交给界面显示
    agent.thinking = False
    model.script.append(["关了思考"])
    agent.run("再问", lambda e: None, lambda t, a: "once")
    assert model.seen_budgets[-1] == 0  # /think off 之后请求不再带思考


def test_openai_reasoning_is_display_only() -> None:
    lines = [
        'data: {"choices": [{"delta": {"reasoning_content": "用户想要"}}]}',
        'data: {"choices": [{"delta": {"reasoning": "一个答案"}}]}',
        'data: {"choices": [{"delta": {"content": "答案是 42"}, "finish_reason": "stop"}]}',
        "data: [DONE]",
    ]
    events = list(parse_openai_sse(iter(lines)))
    assert [e.text for e in events if isinstance(e, ThinkingDelta)] == ["用户想要", "一个答案"]
    assert not any(isinstance(e, ThinkingBlock) for e in events)  # 不产生思考块：不会存进历史再传回 DeepSeek
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["答案是 42"]


def test_ui_folds_thinking_and_think_command(tmp_path: Path) -> None:
    console = Console(record=True, width=120, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([])), console=console)
    repl.on_event(ThinkingDelta("第一步先读代码，"))
    repl.on_event(ThinkingDelta("再看测试。"))
    repl.on_event(TextDelta("结论：没问题"))
    repl.on_event(TurnDone({}, rounds=1))
    shown = console.export_text()
    assert "✻ 思考了" in shown and "/think show" in shown and "第一步先读代码" not in shown  # 折叠成一行，不刷屏
    assert repl.last_thought == "第一步先读代码，再看测试。"
    repl.handle_command("/think")
    assert repl.agent.thinking is True
    repl.handle_command("/think 500")  # 低于下限：不改
    repl.handle_command("/think 16000")
    assert repl.agent.thinking_budget == 16000
    repl.handle_command("/think off")
    assert repl.agent.thinking is False
    repl.handle_command("/think show")
    shown = console.export_text()
    assert "至少 1024" in shown and "第一步先读代码" in shown

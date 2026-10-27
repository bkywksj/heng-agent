"""模型客户端：不连网，用录好的事件流和 httpx.MockTransport 测两种协议的解析、消息转换、报错与请求头。"""

from __future__ import annotations

import json

import httpx
import pytest

from heng_agent.core.errors import ModelError
from heng_agent.core.types import TextDelta, ToolCall, TurnEnd
from heng_agent.model.client import (
    AnthropicClient,
    OpenAIClient,
    make_client,
    parse_openai_sse,
    parse_sse,
    to_openai_messages,
)
from heng_agent.model.providers import Profile


def sse(*events: dict) -> list[str]:
    lines = []
    for e in events:
        lines += [f"event: {e.get('type', '')}", f"data: {json.dumps(e, ensure_ascii=False)}", ""]
    return lines


TOOL_STREAM = sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 42}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "我来读"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "一下。"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"pa'}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'th": "中文.txt"}'}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 17}},
    {"type": "message_stop"},
)  # fmt: skip


def test_anthropic_text_and_split_tool_arguments() -> None:
    assert list(parse_sse(iter(TOOL_STREAM))) == [
        TextDelta("我来读"),
        TextDelta("一下。"),
        ToolCall(id="toolu_1", name="read_file", input={"path": "中文.txt"}),  # 分两段的参数拼完再解析
        TurnEnd("tool_use", {"input_tokens": 42, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 17}),
    ]


def test_anthropic_errors_and_truncation() -> None:
    with pytest.raises(ModelError, match="overloaded"):
        list(parse_sse(iter(sse({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}))))
    with pytest.raises(ModelError, match="断了"):
        list(parse_sse(iter(TOOL_STREAM[:6])))


OPENAI_STREAM = [
    'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"好的，"}}]}',
    'data: {"choices":[{"index":0,"delta":{"content":"我看看。"}}]}',
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"grep","arguments":"{\\"pat"}}]}}]}',
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"tern\\": \\"def\\"}"}}]}}]}',
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":1,"id":"call_b","type":"function","function":{"name":"glob","arguments":"{}"}}]}}]}',
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":30,"completion_tokens":9}}',
    "data: [DONE]",
]


def test_openai_stream_accumulates_tool_calls_by_index() -> None:
    assert list(parse_openai_sse(iter(OPENAI_STREAM))) == [
        TextDelta("好的，"),
        TextDelta("我看看。"),
        ToolCall("call_a", "grep", {"pattern": "def"}),
        ToolCall("call_b", "glob", {}),
        TurnEnd("tool_use", {"input_tokens": 30, "output_tokens": 9}),
    ]
    with pytest.raises(ModelError, match="DONE"):
        list(parse_openai_sse(iter(OPENAI_STREAM[:3])))


def test_messages_convert_to_openai_format() -> None:
    history = [
        {"role": "user", "content": [{"type": "text", "text": "跑测试"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "好"},
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "pytest"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "退出码 1", "is_error": True},
        ]},
    ]  # fmt: skip
    out = to_openai_messages("系统", history)
    assert out[0] == {"role": "system", "content": "系统"}
    assert out[1] == {"role": "user", "content": "跑测试"}
    call = out[2]["tool_calls"][0]
    assert (
        out[2]["content"] == "好"
        and call["id"] == "t1"
        and json.loads(call["function"]["arguments"]) == {"command": "pytest"}
    )
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "[错误] 退出码 1"}


def _profile(protocol: str, auth: str = "auto", **kw: object) -> Profile:
    return Profile(protocol=protocol, base_url=kw.pop("base_url", "https://api.example.com"), model="m",
                   api_key="sk-test", auth=auth, **kw)  # type: ignore[arg-type]  # fmt: skip


def _capture(stream_lines: list[str]) -> tuple[dict, httpx.MockTransport]:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(request.url), request.headers, json.loads(request.content)
        return httpx.Response(200, text="\n".join(stream_lines))

    return seen, httpx.MockTransport(handler)


def test_anthropic_request_url_headers_and_key_only_in_header() -> None:
    seen, transport = _capture(TOOL_STREAM)
    client = make_client(
        _profile("anthropic", "x-api-key", base_url="https://api.deepseek.com/anthropic"), 100, transport
    )
    assert isinstance(client, AnthropicClient)
    list(client.stream("系统", [{"role": "user", "content": "hi"}], [{"name": "glob"}]))
    assert seen["url"] == "https://api.deepseek.com/anthropic/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-test" and "authorization" not in seen["headers"]
    assert seen["body"]["stream"] is True and "sk-test" not in json.dumps(seen["body"])


def test_openai_request_shape_and_bearer_auth() -> None:
    seen, transport = _capture(OPENAI_STREAM)
    client = make_client(
        _profile("openai", "bearer", base_url="https://dashscope.example/compatible-mode/v1/"), 100, transport
    )
    assert isinstance(client, OpenAIClient)
    list(
        client.stream(
            "系统", [{"role": "user", "content": "hi"}], [{"name": "glob", "input_schema": {"type": "object"}}]
        )
    )
    assert seen["url"] == "https://dashscope.example/compatible-mode/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer sk-test" and "x-api-key" not in seen["headers"]
    assert seen["body"]["tools"][0]["function"]["name"] == "glob" and seen["body"]["stream_options"]["include_usage"]


def test_http_error_and_connection_refused() -> None:
    client = make_client(_profile("anthropic"), 100, httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
    with pytest.raises(ModelError, match="401"):
        list(client.stream("", [], []))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    local = make_client(_profile("anthropic", base_url="http://127.0.0.1:8765"), 100, httpx.MockTransport(refuse))
    with pytest.raises(ModelError, match="heng serve"):
        list(local.stream("", [], []))


def _flaky(responses: list[httpx.Response]) -> tuple[list[int], httpx.MockTransport]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return responses[min(len(calls), len(responses)) - 1]

    return calls, httpx.MockTransport(handler)


def _client_with(transport: httpx.MockTransport) -> tuple[AnthropicClient, list[float], list[str]]:
    client = make_client(_profile("anthropic"), 100, transport)
    slept: list[float] = []
    notes: list[str] = []
    client.sleep = slept.append  # type: ignore[method-assign]
    client.on_retry = notes.append
    return client, slept, notes  # type: ignore[return-value]


def test_retries_overload_then_succeeds_honoring_retry_after() -> None:
    busy = httpx.Response(529, headers={"retry-after": "3"}, text="overloaded")
    calls, transport = _flaky(
        [busy, httpx.Response(429, text="slow down"), httpx.Response(200, text="\n".join(TOOL_STREAM))]
    )
    client, slept, notes = _client_with(transport)
    events = list(client.stream("", [], []))
    assert isinstance(events[-1], TurnEnd) and len(calls) == 3
    assert slept == [3.0, 2.0]  # 第一次听 Retry-After，第二次按指数退避（2**1）
    assert len(notes) == 2 and "529" in notes[0] and "重试" in notes[0]


def test_non_retryable_and_exhausted_errors() -> None:
    calls, transport = _flaky([httpx.Response(401, text="bad key")])
    client, slept, _ = _client_with(transport)
    with pytest.raises(ModelError, match="401"):
        list(client.stream("", [], []))
    assert len(calls) == 1 and slept == []  # 密钥错了重试也没用
    calls, transport = _flaky([httpx.Response(503, text="down")])
    client, slept, _ = _client_with(transport)
    with pytest.raises(ModelError, match="503"):
        list(client.stream("", [], []))
    assert len(calls) == 5 and slept == [1.0, 2.0, 4.0, 8.0]  # 最多重试 4 次


def test_no_retry_once_text_has_been_streamed() -> None:
    # 已经吐出文字后断流：不能重试，否则用户看到的半段回复和重新生成的对不上
    partial = "\n".join(TOOL_STREAM[:12])  # 每个事件 3 行：message_start、块开始、两段文字，没有 message_stop
    calls, transport = _flaky([httpx.Response(200, text=partial)])
    client, slept, _ = _client_with(transport)
    got: list = []
    with pytest.raises(ModelError, match="断了"):
        for e in client.stream("", [], []):
            got.append(e)
    assert got == [TextDelta("我来读"), TextDelta("一下。")] and len(calls) == 1 and slept == []

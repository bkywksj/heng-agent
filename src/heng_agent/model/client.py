"""模型客户端：Anthropic Messages 与 OpenAI Chat Completions 两种协议。

两种协议的流式事件都归一成 TextDelta / ToolCall / TurnEnd。

循环里的消息一律是 Anthropic 格式；走 OpenAI 协议时由 to_openai_messages 在发送前转换。
循环只依赖 `ModelClient` 这个接口；测试里换成脚本化的假模型（tests/fake_model.py）。
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import httpx

from heng_agent.core.errors import ModelError
from heng_agent.core.types import Message, ModelEvent, TextDelta, ThinkingBlock, ThinkingDelta, ToolCall, TurnEnd
from heng_agent.model.providers import Profile

ANTHROPIC_VERSION = "2023-06-01"


class ModelClient(Protocol):
    def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> Iterator[ModelEvent]:
        """发一次请求，流式吐出事件，最后一个一定是 TurnEnd。"""
        ...


def _data_lines(lines: Iterator[str]) -> Iterator[str]:
    for line in lines:
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                yield payload


def _parse_args(raw: str, name: str) -> dict[str, Any]:
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise ModelError(f"模型给的工具参数不是合法 JSON（{name}）：{exc}") from exc


# ---------- Anthropic ----------


def parse_sse(lines: Iterator[str]) -> Iterator[ModelEvent]:
    """Anthropic SSE → 模型事件。内容块按 index 区分：文字增量立即吐出；工具的 input_json_delta 攒到块结束再解析。"""
    blocks: dict[int, dict[str, Any]] = {}
    usage: dict[str, int] = {}
    stop_reason = "end_turn"
    for payload in _data_lines(lines):
        data = json.loads(payload)
        kind = data.get("type")
        if kind == "message_start":
            u = data.get("message", {}).get("usage", {})
            usage["input_tokens"] = int(u.get("input_tokens", 0))
            # 开了提示缓存时 input_tokens 只是没命中的部分，命中与写入的另外报：不记上会低估上下文与费用
            usage["cache_read_tokens"] = int(u.get("cache_read_input_tokens") or 0)
            usage["cache_write_tokens"] = int(u.get("cache_creation_input_tokens") or 0)
        elif kind == "content_block_start":
            block = data["content_block"]
            if block["type"] == "thinking":
                blocks[data["index"]] = {"type": "thinking", "thinking": "", "signature": ""}
            elif block["type"] == "redacted_thinking":  # 服务不给看的思考（加密）：原样存、原样传回
                blocks[data["index"]] = {"type": "redacted_thinking", "data": block.get("data", "")}
            else:
                blocks[data["index"]] = {
                    "type": block["type"],
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "json": "",
                }
        elif kind == "content_block_delta":
            delta = data["delta"]
            if delta["type"] == "text_delta":
                yield TextDelta(delta["text"])
            elif delta["type"] == "input_json_delta":
                blocks[data["index"]]["json"] += delta.get("partial_json", "")
            elif delta["type"] == "thinking_delta":
                blocks[data["index"]]["thinking"] += delta.get("thinking", "")
                yield ThinkingDelta(delta.get("thinking", ""))
            elif delta["type"] == "signature_delta":
                blocks[data["index"]]["signature"] += delta.get("signature", "")
        elif kind == "content_block_stop":
            block = blocks.pop(data["index"], None)
            if block and block["type"] == "tool_use":
                yield ToolCall(id=block["id"], name=block["name"], input=_parse_args(block["json"], block["name"]))
            elif block and block["type"] in ("thinking", "redacted_thinking"):
                yield ThinkingBlock(block)
        elif kind == "message_delta":
            stop_reason = data.get("delta", {}).get("stop_reason") or stop_reason
            usage["output_tokens"] = int(data.get("usage", {}).get("output_tokens", 0))
        elif kind == "error":
            err = data.get("error", {})
            retryable = err.get("type") in ("overloaded_error", "rate_limit_error", "api_error")
            raise ModelError(
                f"模型服务报错：{err.get('type', '')} {err.get('message', '')}".strip(), retryable=retryable
            )
        elif kind == "message_stop":
            yield TurnEnd(stop_reason=stop_reason, usage=usage)
            return
    raise ModelError("模型的流在 message_stop 之前就断了")


# ---------- OpenAI ----------


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    """Anthropic 格式 → OpenAI Chat Completions 格式。

    - 用户消息里的 tool_result 块各变成一条 role=tool 消息（OpenAI 要求紧跟在带 tool_calls 的助手消息后面）；
    - 助手的 tool_use 块变成 tool_calls，参数序列化成 JSON 字符串。
    """
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for msg in messages:
        content = msg["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        texts = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if msg["role"] == "assistant":
            calls = [
                {"id": b["id"], "type": "function",
                 "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}}
                for b in blocks if b.get("type") == "tool_use"
            ]  # fmt: skip
            item: dict[str, Any] = {"role": "assistant", "content": texts or None}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
            continue
        for b in blocks:
            if b.get("type") == "tool_result":
                body = b.get("content", "")
                if b.get("is_error"):
                    body = f"[错误] {body}"  # OpenAI 的 tool 消息没有 is_error 字段，写进正文让模型知道
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": body})
        images = [b for b in blocks if b.get("type") == "image"]
        if images:  # 有图片时 content 改成分段数组：文字 + data URL 形式的图片
            parts: list[dict[str, Any]] = [{"type": "text", "text": texts}] if texts else []
            for img in images:
                src = img["source"]
                parts.append(
                    {"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}}
                )
            out.append({"role": "user", "content": parts})
        elif texts:
            out.append({"role": "user", "content": texts})
    return out


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema", {"type": "object"})}}
            for t in tools]  # fmt: skip


_FINISH = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}


def parse_openai_sse(lines: Iterator[str]) -> Iterator[ModelEvent]:
    """OpenAI 流 → 模型事件。工具调用按 index 累积 id / name / arguments 分片，流结束时一次吐出。"""
    calls: dict[int, dict[str, str]] = {}
    usage: dict[str, int] = {}
    finish = "stop"
    for payload in _data_lines(lines):
        if payload == "[DONE]":
            break
        data = json.loads(payload)
        if "error" in data:
            err = data["error"]
            raise ModelError(f"模型服务报错：{err.get('message', err) if isinstance(err, dict) else err}")
        if data.get("usage"):
            usage = {"input_tokens": int(data["usage"].get("prompt_tokens", 0)),
                     "output_tokens": int(data["usage"].get("completion_tokens", 0))}  # fmt: skip
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            # 推理模型的思考过程：DeepSeek / 通义在 reasoning_content，OpenRouter 在 reasoning。
            # 只给界面显示、不进历史：DeepSeek 明确说把它传回去会报错
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                yield ThinkingDelta(reasoning)
            if delta.get("content"):
                yield TextDelta(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(int(tc.get("index", 0)), {"id": "", "name": "", "args": ""})
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function") or {}
                slot["name"] = fn.get("name") or slot["name"]
                slot["args"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    else:
        raise ModelError("模型的流在 [DONE] 之前就断了")
    for index in sorted(calls):
        c = calls[index]
        yield ToolCall(id=c["id"] or f"call_{index}", name=c["name"], input=_parse_args(c["args"], c["name"]))
    yield TurnEnd(stop_reason="tool_use" if calls else _FINISH.get(finish, finish), usage=usage)


# ---------- 客户端 ----------

#: 等一下再试多半能成的 HTTP 状态：限流、超时、服务端错误、Anthropic 的过载（529），
#: 以及 Cloudflare 的 520–525、527（CDN 连不上源站、源站超时、与源站的 SSL 握手失败）——很多中转站架在
#: Cloudflare 后面，实测遇到过 522、525。526（源站证书无效）不是临时问题，重试也没用，不在其中
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 527, 529})
MAX_RETRIES = 4


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None


class _HttpClient:
    def __init__(self, profile: Profile, max_tokens: int, transport: httpx.BaseTransport | None = None) -> None:
        self.profile = profile
        self.max_tokens = max_tokens
        # 长回复可能持续几分钟：读超时放宽，连接超时保持短，服务没起来时能快速报错
        self.http = httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0), transport=transport)
        self._cancelled = threading.Event()  # 用户按了 ESC：这次请求作废，不重试
        self._active: httpx.Response | None = None  # 正在读的响应，取消时从别的线程关掉它
        self.sleep: Callable[[float], None] = self._cancellable_sleep  # 测试里换成不真睡的版本
        #: 重试前告诉用户发生了什么（界面层设置；不设就静默重试）
        self.on_retry: Callable[[str], None] | None = None
        #: 思考预算（token），0 = 不开思考。由智能体在每次请求前按 /think 的开关设置
        self.thinking_budget = 0

    def cancel(self) -> None:
        """从别的线程取消当前请求（ESC）。

        主线程此时多半卡在网络读取上，发中断信号打不断它；直接关掉正在读的响应，读取立刻报错退出，
        再由 _post_stream 把那个错误换成 KeyboardInterrupt（而不是当成网络抖动去重试）。
        """
        self._cancelled.set()
        response = self._active
        if response is not None:
            with contextlib.suppress(Exception):  # 响应可能正好读完、已关，都无妨
                response.close()

    def _cancellable_sleep(self, seconds: float) -> None:
        if self._cancelled.wait(seconds):  # 重试前的等待也能被 ESC 打断
            raise KeyboardInterrupt

    def _post_stream(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> Iterator[str]:
        try:
            yield from self._post_stream_inner(url, body, headers)
        except Exception as exc:
            if self._cancelled.is_set():  # 是用户取消导致的读取报错：换成中断，不走重试
                raise KeyboardInterrupt from exc
            raise

    def _post_stream_inner(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> Iterator[str]:
        try:
            with self.http.stream("POST", url, json=body, headers=headers) as resp:
                self._active = resp
                if self._cancelled.is_set():
                    raise KeyboardInterrupt
                if resp.status_code >= 400:
                    detail = resp.read().decode("utf-8", errors="replace")[:500]
                    raise ModelError(
                        f"模型服务返回 {resp.status_code}：{detail}",
                        retryable=resp.status_code in RETRY_STATUS,
                        retry_after=_retry_after(resp),
                    )
                yield from resp.iter_lines()
        except httpx.ConnectError as exc:
            hint = "先启动 heng serve" if "127.0.0.1" in url or "localhost" in url else "检查网络与地址"
            # 本机服务没启动时重试也没用，直接报；远程的连接失败可能是网络抖动，值得重试
            remote = "127.0.0.1" not in url and "localhost" not in url
            raise ModelError(f"连不上模型服务 {self.profile.base_url}：{hint}", retryable=remote) from exc
        except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise ModelError(f"和模型服务的连接中断：{type(exc).__name__}", retryable=True) from exc
        finally:
            self._active = None

    def _with_retry(self, attempt: Callable[[], Iterator[ModelEvent]]) -> Iterator[ModelEvent]:
        """失败且还没吐出任何事件时重试（指数退避 1/2/4/8 秒，服务给了 Retry-After 就听它的）。

        已经开始往外吐文字后就不再重试：用户已经看到半段回复，重新生成的不会一样，屏幕与历史会对不上。
        用户按 ESC 取消的不重试（_post_stream 里已换成 KeyboardInterrupt）。
        """
        self._cancelled.clear()  # 新的一次请求：上次的取消标记作废
        for n in range(MAX_RETRIES + 1):
            started = False
            try:
                for event in attempt():
                    started = True
                    yield event
                return
            except ModelError as exc:
                if self._cancelled.is_set():  # 关掉响应后读取「提前结束」会报成流断了：也是用户取消
                    raise KeyboardInterrupt from exc
                if started or not exc.retryable or n == MAX_RETRIES:
                    raise
                wait = exc.retry_after if exc.retry_after is not None else float(2**n)
                wait = min(wait, 60.0)
                if self.on_retry is not None:
                    self.on_retry(f"{exc}；{wait:g} 秒后重试（第 {n + 1}/{MAX_RETRIES} 次）")
                self.sleep(wait)


class AnthropicClient(_HttpClient):
    def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> Iterator[ModelEvent]:
        base = self.profile.base_url.rstrip("/").removesuffix("/v1")
        headers = {"anthropic-version": ANTHROPIC_VERSION, **self.profile.auth_headers()}
        sys_field: Any = system
        if self.profile.prompt_cache:
            sys_blocks, messages = with_cache_marks(system, messages)
            sys_field = sys_blocks or system  # 没有系统提示词时保持空字符串，别发 "system": []
        max_tokens = self.max_tokens
        extra: dict[str, Any] = {}
        if self.thinking_budget > 0:
            # max_tokens 必须大于思考预算（否则服务拒收）：思考之外还要留出回答的空间
            extra["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_budget}
            max_tokens = self.thinking_budget + self.max_tokens
        else:
            messages = strip_thinking(messages)  # 关了思考：历史里的思考块不再发
        body = {"model": self.profile.model, "max_tokens": max_tokens, "system": sys_field,
                "messages": messages, "tools": tools, "stream": True, **extra}  # fmt: skip
        yield from self._with_retry(lambda: parse_sse(self._post_stream(f"{base}/v1/messages", body, headers)))


CACHE_MARK = {"type": "ephemeral"}
THINKING_TYPES = ("thinking", "redacted_thinking")


def strip_thinking(messages: list[Message]) -> list[Message]:
    """去掉历史里的思考块（关掉思考后发请求用）。只拷贝有思考块的消息，不改对话历史本身。"""
    out: list[Message] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list) and any(b.get("type") in THINKING_TYPES for b in content):
            kept = [b for b in content if b.get("type") not in THINKING_TYPES]
            msg = {**msg, "content": kept or [{"type": "text", "text": "（思考）"}]}  # 助手消息不能为空
        out.append(msg)
    return out


def with_cache_marks(system: str, messages: list[Message]) -> tuple[list[dict[str, Any]], list[Message]]:
    """Anthropic 提示缓存：在系统提示词末尾和最后一条消息的最后一块打上缓存断点。

    请求的前缀按 工具 → 系统提示词 → 消息 的顺序缓存：第一个断点盖住工具声明与系统提示词（每次都一样），
    第二个盖住到目前为止的整段对话——下一次请求只在末尾多出几条，前面整段命中缓存，按 0.1 倍计价。
    只拷贝最后一条消息再加标记：不能改到对话历史本身（否则标记会越积越多，超过上限 4 个被拒）。
    """
    sys_blocks = [{"type": "text", "text": system, "cache_control": CACHE_MARK}] if system else []
    if not messages:
        return sys_blocks, messages
    last = dict(messages[-1])
    content = last.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [dict(b) for b in content or []]
    if blocks:
        blocks[-1]["cache_control"] = CACHE_MARK
        last["content"] = blocks
    return sys_blocks, [*messages[:-1], last]


class OpenAIClient(_HttpClient):
    def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> Iterator[ModelEvent]:
        body: dict[str, Any] = {
            "model": self.profile.model,
            "max_tokens": self.max_tokens,
            "messages": to_openai_messages(system, messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
        url = f"{self.profile.base_url.rstrip('/')}/chat/completions"
        headers = self.profile.auth_headers()
        yield from self._with_retry(lambda: parse_openai_sse(self._post_stream(url, body, headers)))


def make_client(profile: Profile, max_tokens: int, transport: httpx.BaseTransport | None = None) -> _HttpClient:
    cls = AnthropicClient if profile.protocol == "anthropic" else OpenAIClient
    return cls(profile, max_tokens, transport)


def list_models(profile: Profile, transport: httpx.BaseTransport | None = None) -> list[str]:
    """向服务商要可用模型列表（/config provider 让用户直接挑，不用记模型名）。

    Anthropic 协议：{base}/v1/models；OpenAI 协议：{base}/models。各家都支持这个接口，中转站通常也转发。
    """
    base = profile.base_url.rstrip("/")
    if profile.protocol == "anthropic":
        url, headers = f"{base.removesuffix('/v1')}/v1/models", {"anthropic-version": ANTHROPIC_VERSION}
    else:
        url, headers = f"{base}/models", {}
    headers.update(profile.auth_headers())
    try:
        with httpx.Client(timeout=20.0, transport=transport) as http:
            resp = http.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ModelError(f"取模型列表失败：{type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise ModelError(f"取模型列表失败：服务返回 {resp.status_code} {resp.text[:200]}")
    try:
        items = resp.json().get("data", [])
    except ValueError as exc:
        raise ModelError("取模型列表失败：返回的不是 JSON") from exc
    return sorted({str(m["id"]) for m in items if isinstance(m, dict) and m.get("id")})

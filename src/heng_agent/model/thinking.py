"""思考的开关与强度：按模型决定请求里怎么写。

以前只有一种写法 `thinking: {type: "enabled", budget_tokens: N}`，但 Claude 新一代模型已经不认它：
- Claude 4.6 以后改成「自适应思考」`thinking: {type: "adaptive"}`，想多想少由 `output_config.effort` 控制
  （low / medium / high / xhigh / max）；budget_tokens 在 Sonnet 5、Opus 4.7 及以后、Fable 上直接返回 400；
- 其中 Opus 5.5、Sonnet 5.5、Fable、Mythos 的思考**关不掉**（发 disabled 也是 400），只能把强度调到 low；
- 新模型默认不返回思考的文字（display 默认 omitted），要显示思考得写 `display: "summarized"`。
更早的 Claude、以及 DeepSeek、Kimi、GLM 这类「兼容 Anthropic 协议」的服务仍用 budget_tokens：强度换算成预算，
不发 effort（它们认不认没有把握，发了可能被拒）。OpenAI 协议用 reasoning_effort（只有 low / medium / high）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
EFFORT_NAME = {"low": "低", "medium": "中", "high": "高", "xhigh": "很高", "max": "最高"}
#: 只认预算的模型：强度换算成思考预算（token）
EFFORT_BUDGET = {"low": 2048, "medium": 8000, "high": 16000, "xhigh": 32000, "max": 64000}
#: Opus 4.6 / Sonnet 4.6 还没有 xhigh（Opus 4.7 才加的）：按 high 发
NO_XHIGH = ("opus-4-6", "sonnet-4-6")

#: always：思考关不掉；adaptive：自适应思考、可以关；budget：老写法 budget_tokens
Style = Literal["always", "adaptive", "budget"]

_CLAUDE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:[-.](\d+))?")


def style(model: str) -> Style:
    """按模型名判断用哪种写法。认模型名里的「claude-家族-主版本-次版本」：
    中转站常用的 claude-sonnet-5、带 [1m] 后缀的也认。"""
    match = _CLAUDE.search(model.lower())
    if match is None:
        return "budget"  # DeepSeek、Kimi、GLM 等：兼容 Anthropic 协议的老写法
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    if minor >= 10:
        minor = 0  # 「claude-sonnet-4-20250514」这类带日期的老名字：后面那串是日期不是版本
    version = major + minor / 10
    if family in ("fable", "mythos"):
        return "always"
    if family in ("opus", "sonnet"):
        if version >= 5.5:
            return "always"
        return "adaptive" if version >= 4.6 else "budget"
    return "adaptive" if version >= 5.5 else "budget"  # haiku：5.5 起自适应（能关），4.5 及以前用预算


def can_disable(model: str) -> bool:
    return style(model) != "always"


@dataclass
class Request:
    """一次 Anthropic 请求里和思考有关的部分。"""

    body: dict[str, Any] = field(default_factory=dict)  # 并进请求体的字段（thinking、output_config）
    extra_tokens: int = 0  # max_tokens 要多留的量：思考也算在 max_tokens 里，不加的话想一会儿回答就被截断
    strip_history: bool = False  # 发之前去掉历史里的思考块


def anthropic(model: str, on: bool, effort: str | None, budget: int) -> Request:
    """on：用户开没开思考；effort：用户设的强度（None = 服务的默认）；budget：/think 16000 设的预算（老写法用）。"""
    kind = style(model)
    if kind == "budget":
        if not on:
            return Request(strip_history=True)  # 关了思考：历史里的思考块不再发
        tokens = EFFORT_BUDGET[effort] if effort else budget
        return Request({"thinking": {"type": "enabled", "budget_tokens": tokens}}, extra_tokens=tokens)
    if effort == "xhigh" and any(name in model.lower() for name in NO_XHIGH):
        effort = "high"
    if not on and kind == "adaptive":
        # 能关的：明确发 disabled（不发的话 Sonnet 5、Opus 5 默认照样在想）。关着思考时 xhigh / max 会被拒，压到 high
        capped = "high" if effort in ("xhigh", "max") else effort
        body: dict[str, Any] = {"thinking": {"type": "disabled"}}
        if capped:
            body["output_config"] = {"effort": capped}
        return Request(body)
    if not on:  # 关不掉的：不带 thinking，强度压到 low，尽量少想（不去掉历史里的思考块：改历史会让后面的思考作废）
        return Request({"output_config": {"effort": "low"}}, extra_tokens=EFFORT_BUDGET["low"])
    body = {"thinking": {"type": "adaptive", "display": "summarized"}}  # summarized：要显示思考的文字
    if effort:
        body["output_config"] = {"effort": effort}
    return Request(body, extra_tokens=EFFORT_BUDGET[effort or "high"])


def openai(on: bool, effort: str | None) -> dict[str, Any]:
    """OpenAI 协议：只在开着思考、又明确设了强度时发 reasoning_effort（很多兼容 OpenAI 的服务不认这个字段）。"""
    if not on or not effort:
        return {}
    return {"reasoning_effort": "high" if effort in ("xhigh", "max") else effort}


def describe(model: str, on: bool, effort: str | None) -> str:
    """给人看的一句话：/status、/think、/effort 用。"""
    level = f"强度 {effort}（{EFFORT_NAME[effort]}）" if effort else "强度用服务默认"
    if on:
        return f"开 · {level}"
    if not can_disable(model):
        return "关不掉（这个模型总会思考），已按 low 强度少想"
    return "关"


def bar_label(model: str, on: bool, effort: str | None) -> str | None:
    """状态栏里的一小段：开着显示「✻ high」，关不掉的显示「✻ low」，关着不显示。"""
    if on:
        return f"✻ {effort}" if effort else "✻ 思考"
    return None if can_disable(model) else "✻ low"

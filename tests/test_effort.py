"""思考强度：按模型选写法（新 Claude 用 adaptive + effort、关不掉的压到 low、别的用 budget_tokens、
OpenAI 用 reasoning_effort），/effort 命令、状态栏与 /status 的显示、ACP 的思考下拉框。

以前只有 `thinking: {type: "enabled", budget_tokens}` 一种写法：Sonnet 5、Opus 4.7 及以后、Fable 上会被直接拒收（400）；
关掉思考时不带参数，Sonnet 5 / Opus 5 默认照样在想。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.model import thinking
from heng_agent.model.client import make_client
from heng_agent.model.providers import Profile
from heng_agent.ui.prompt import toolbar
from heng_agent.ui.repl import Repl


@pytest.mark.parametrize(("model", "kind"), [
    ("claude-opus-5-5", "always"), ("claude-sonnet-5-5", "always"), ("claude-fable-5-1[1m]", "always"),
    ("claude-mythos-5", "always"), ("claude-opus-6", "always"),
    ("claude-sonnet-5", "adaptive"), ("claude-opus-5", "adaptive"), ("claude-opus-4-8", "adaptive"),
    ("claude-sonnet-4-6", "adaptive"), ("claude-haiku-5-5", "adaptive"),
    ("claude-haiku-4-5", "budget"), ("claude-sonnet-4-20250514", "budget"), ("claude-3-7-sonnet", "budget"),
    ("deepseek-v4-pro", "budget"), ("kimi-k2.7-code", "budget"), ("glm-5.2", "budget"),
])  # fmt: skip
def test_style_by_model(model: str, kind: str) -> None:
    assert thinking.style(model) == kind


def _bodies(model: str, protocol: str = "anthropic") -> tuple[list[dict[str, Any]], Any]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"type": "message_stop"}\n\ndata: [DONE]\n\n')

    client = make_client(Profile(protocol=protocol, base_url="http://x", model=model), 4096,  # type: ignore[arg-type]
                         transport=httpx.MockTransport(handler))  # fmt: skip
    return bodies, client


def _send(client: Any, budget: int, effort: str | None) -> None:
    client.thinking_budget, client.effort = budget, effort
    history = [{"role": "user", "content": [{"type": "text", "text": "x"}]},
               {"role": "assistant", "content": [{"type": "thinking", "thinking": "想", "signature": "s"},
                                                 {"type": "text", "text": "好"}]},
               {"role": "user", "content": [{"type": "text", "text": "继续"}]}]  # fmt: skip
    list(client.stream("", history, []))


def _has_thinking_block(body: dict[str, Any]) -> bool:
    return any(b.get("type") == "thinking" for m in body["messages"] for b in m["content"] if isinstance(b, dict))


def test_new_claude_uses_adaptive_and_effort_never_budget() -> None:
    bodies, client = _bodies("claude-sonnet-5")
    _send(client, 8000, "high")  # 开 + 强度
    _send(client, 8000, None)  # 开、强度用服务默认
    _send(client, 0, None)  # 关
    on, default, off = bodies
    assert on["thinking"] == {"type": "adaptive", "display": "summarized"}  # summarized：思考的文字才会返回
    assert on["output_config"] == {"effort": "high"} and "budget_tokens" not in json.dumps(on)
    assert on["max_tokens"] == 4096 + thinking.EFFORT_BUDGET["high"]  # 思考的量也算进 max_tokens
    assert "output_config" not in default
    assert off["thinking"] == {"type": "disabled"}  # 能关的明确关掉：不带参数的话 Sonnet 5 默认照样在想
    assert all(_has_thinking_block(b) for b in bodies)  # 新模型不去掉历史里的思考块（改历史会让思考作废）


def test_disabled_thinking_caps_effort_at_high() -> None:
    bodies, client = _bodies("claude-opus-5")
    _send(client, 0, "max")  # 关着思考时 xhigh / max 会被拒
    assert bodies[0]["thinking"] == {"type": "disabled"} and bodies[0]["output_config"] == {"effort": "high"}


def test_always_thinking_models_go_low_when_turned_off() -> None:
    bodies, client = _bodies("claude-opus-5-5")
    _send(client, 0, "xhigh")
    off = bodies[0]
    assert "thinking" not in off  # disabled 在这些模型上是 400：不带
    assert off["output_config"] == {"effort": "low"}  # 关不掉就尽量少想


def test_no_xhigh_on_4_6() -> None:
    bodies, client = _bodies("claude-sonnet-4-6")
    _send(client, 8000, "xhigh")
    assert bodies[0]["output_config"] == {"effort": "high"}  # xhigh 是 Opus 4.7 才加的


def test_budget_models_keep_budget_tokens_and_map_effort() -> None:
    bodies, client = _bodies("deepseek-v4-pro")
    _send(client, 8000, None)
    _send(client, 8000, "max")
    _send(client, 0, "high")
    default, mapped, off = bodies
    assert default["thinking"] == {"type": "enabled", "budget_tokens": 8000} and "output_config" not in default
    assert mapped["thinking"]["budget_tokens"] == thinking.EFFORT_BUDGET["max"] and "output_config" not in mapped
    assert "thinking" not in off and not _has_thinking_block(off)  # 关了：历史里的思考块照旧去掉


def test_openai_reasoning_effort() -> None:
    bodies, client = _bodies("gpt-5.1", "openai")
    _send(client, 8000, "max")
    _send(client, 8000, None)
    _send(client, 0, "low")
    assert bodies[0]["reasoning_effort"] == "high"  # OpenAI 只有 low / medium / high
    assert "reasoning_effort" not in bodies[1] and "reasoning_effort" not in bodies[2]  # 没设强度、关着：都不发


def test_agent_passes_effort_to_client(tmp_path: Any) -> None:
    model = FakeModel([["好"]])
    model.thinking_budget, model.effort = 0, None  # type: ignore[attr-defined]
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h", thinking=True, effort="xhigh"), model)
    agent.run("你好", lambda e: None, lambda t, a: "once")
    assert model.effort == "xhigh" and model.thinking_budget == agent.thinking_budget  # type: ignore[attr-defined]


def test_toolbar_and_describe() -> None:
    def info(**kw: Any) -> str:
        return "".join(t for _, t in toolbar("ask", "m", "w", 0, 1, **kw)).split("\n")[0]

    assert "✻ high" in info(thinking=thinking.bar_label("claude-sonnet-5", True, "high"))
    assert thinking.bar_label("claude-sonnet-5", False, "high") is None  # 关着：不显示
    assert thinking.bar_label("claude-opus-5-5", False, None) == "✻ low"  # 关不掉的照实显示
    assert thinking.describe("claude-opus-5-5", False, None).startswith("关不掉")
    assert thinking.describe("claude-sonnet-5", True, "max") == "开 · 强度 max（最高）"


def _repl(tmp_path: Any, model: str = "claude-sonnet-5") -> tuple[Repl, Console]:
    console = Console(record=True, width=160, force_terminal=False)
    client = FakeModel([])
    client.profile = Profile(protocol="anthropic", base_url="http://x", model=model)  # type: ignore[attr-defined]
    return Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), client), console=console), console


def test_effort_command(tmp_path: Any) -> None:
    repl, console = _repl(tmp_path)
    repl.handle_command("/effort high")
    assert repl.agent.effort == "high" and repl.agent.thinking  # 设强度顺手打开思考
    repl.handle_command("/effort auto")
    assert repl.agent.effort is None
    repl.handle_command("/effort 很多")
    shown = console.export_text()
    assert "开 · 强度 high（高）" in shown and "用法：/effort" in shown


def test_effort_picker(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """default 要传序号：以前传了强度的名字，真实的选择器里报错、被当成「终端用不了选择器」，输入框也退回成逐行输入。"""
    repl, _ = _repl(tmp_path)
    defaults: list[Any] = []

    def fake_choose(title: str, options: list[Any], default: int = 0) -> str:
        defaults.append(default)
        assert isinstance(default, int) and 0 <= default < len(options)  # 和真实的 select.choose 一样的要求
        return "medium"

    monkeypatch.setattr(repl, "_choose", fake_choose)
    repl.handle_command("/effort")
    repl.handle_command("/effort")
    assert repl.agent.effort == "medium" and defaults == [len(thinking.EFFORTS), thinking.EFFORTS.index("medium")]


def test_effort_setting_is_validated(tmp_path: Any) -> None:
    with pytest.raises(ValueError):
        Settings(effort="超级")  # type: ignore[arg-type]
    assert Settings(effort="max").effort == "max"


def test_acp_thinking_dropdown_has_levels(tmp_path: Any) -> None:
    from heng_agent import acp_server

    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h", thinking=True, effort="high"), FakeModel([]))
    session = acp_server.Session(agent=agent, manager=None, provider="heng")  # type: ignore[arg-type]
    thinking_option = next(o for o in acp_server.config_options(session) if o.id == "thinking")
    values = [o.value for o in thinking_option.options]
    assert values == ["off", "on", *thinking.EFFORTS] and thinking_option.current_value == "high"

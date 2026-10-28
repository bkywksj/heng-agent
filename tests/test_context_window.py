"""上下文长度从哪来：配置 → 接口查询 → 按模型名推断 → 默认（model/context_window.py）。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.model import context_window as cw
from heng_agent.model.providers import PRESETS, Profile, load_profiles

M, K200 = 1_000_000, 200_000


@pytest.mark.parametrize(
    ("model", "window"),
    [
        ("claude-sonnet-4-6", M),  # 中转站上的 claude-sonnet-4-6：以前当成 128k，102k 就压缩
        ("claude-opus-5-5", M),
        ("claude-sonnet-4.6-thinking", M),  # 点号、后缀
        ("anthropic/claude-opus-4.8", M),  # OpenRouter 的前缀
        ("claude-fable-5-1", M),
        ("claude-haiku-5-5", M),
        ("claude-opus-5-5[1m]", M),
        ("claude-haiku-4-5-20251001", K200),
        ("claude-sonnet-4-5", K200),
        ("claude-sonnet-4-20250514", K200),  # 后面是日期，不是 4.20250514
        ("claude-3-7-sonnet-20250219", K200),
        ("deepseek-v4-pro", None),  # 没核实过的不猜
        ("gpt-5.1", None),
    ],
)
def test_known_by_model_name(model: str, window: int | None) -> None:
    assert cw.known(model) == window


def _profile(**extra: object) -> Profile:
    return Profile(protocol="anthropic", base_url="https://relay.example", model="claude-sonnet-4-6", **extra)


def test_resolve_priority() -> None:
    assert cw.resolve(_profile(context_window=150_000), 300_000, 128_000) == (150_000, "配置")  # 手写的永远优先
    assert cw.resolve(_profile(), 300_000, 128_000) == (300_000, "接口查询")
    assert cw.resolve(_profile(), None, 128_000) == (M, "按模型名推断")
    unknown = Profile(protocol="openai", base_url="https://x", model="mystery-model")
    assert cw.resolve(unknown, None, 128_000) == (128_000, "默认")
    assert cw.resolve(None, None, 64_000) == (64_000, "默认")


def test_profile_from_preset_is_not_treated_as_configured(tmp_path: Path) -> None:
    """配置档从预设合并而来：没写 context_window 就是「没配置」（以前默认值 128k 混在里面，分不出来）。"""
    home = tmp_path / "home"
    home.mkdir()
    profiles = {"relay": {"preset": "anthropic", "base_url": "https://relay.example", "model": "claude-opus-5-5"},
                "fixed": {"preset": "anthropic", "context_window": 150_000}}  # fmt: skip
    (home / "config.json").write_text(json.dumps({"profiles": profiles}), encoding="utf-8")
    loaded, _ = load_profiles(home)
    assert loaded["relay"].context_window is None and loaded["fixed"].context_window == 150_000
    assert PRESETS["anthropic"].context_window is None  # 官方的按模型查 / 推断，不写死 200k


def _transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(request.url.path, httpx.Response(404))

    return httpx.MockTransport(handler)


def test_fetch_anthropic_models_endpoint() -> None:
    ok = _transport({"/v1/models/claude-sonnet-4-6": httpx.Response(200, json={"max_input_tokens": 1_000_000})})
    assert cw.fetch(_profile(), transport=ok) == M
    assert cw.fetch(_profile(), transport=_transport({})) is None  # 中转站不转发：查不到不算错
    html = _transport({"/v1/models/claude-sonnet-4-6": httpx.Response(200, text="<html>")})
    assert cw.fetch(_profile(), transport=html) is None


def test_fetch_openai_model_list() -> None:
    profile = Profile(protocol="openai", base_url="https://openrouter.ai/api/v1", model="deepseek/deepseek-v4-pro")
    data = {
        "data": [{"id": "other", "context_length": 8000}, {"id": "deepseek/deepseek-v4-pro", "context_length": 163_840}]
    }
    assert cw.fetch(profile, transport=_transport({"/api/v1/models": httpx.Response(200, json=data)})) == 163_840
    bare = {"data": [{"id": "deepseek/deepseek-v4-pro"}]}
    assert cw.fetch(profile, transport=_transport({"/api/v1/models": httpx.Response(200, json=bare)})) is None


def test_agent_uses_fetched_value_for_its_model_only(tmp_path: Path) -> None:
    client = FakeModel([])
    client.profile = _profile()  # type: ignore[attr-defined]
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), client)
    assert agent.context_window_info == (M, "按模型名推断")
    agent.fetched_windows[("https://relay.example", "claude-sonnet-4-6")] = 400_000
    assert agent.context_window_info == (400_000, "接口查询")
    client.profile = _profile().model_copy(update={"model": "mystery"})  # type: ignore[attr-defined]  # 换了模型：查到的是上一个的，不用
    assert agent.context_window_info == (128_000, "默认")

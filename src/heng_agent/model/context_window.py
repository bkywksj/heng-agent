"""模型的上下文长度从哪来：配置 → 接口查询 → 按模型名推断 → 默认。

以前只看配置档的 context_window，没写就是 128k：中转站上的 claude-sonnet-4-6（实际 1M）在 102k 就开始自动压缩、
状态栏的百分比也偏高。现在：
1. 配置档里写了 context_window：一直用它（推断错了可以手动纠正，/config set profiles.名字.context_window 数字）；
2. 接口查得到：Anthropic 协议问 /v1/models/{模型}（官方返回 max_input_tokens），OpenAI 协议看模型列表里
   有没有带 context_length 之类的字段（OpenRouter 带）；中转站多半查不到，查不到不算错；
3. 按模型名推断：只收录核对过官方文档的（目前是 Claude，见 claude-api 技能的模型表，2026-10）；
4. 都没有：用设置里的 context_window（默认 128k）。

宁小勿大：说小了只是压缩早一点；说大了请求会超长被拒。所以拿不准的型号不进表。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx

from heng_agent.core.net import http_client

if TYPE_CHECKING:
    from heng_agent.model.providers import Profile

M = 1_000_000
K200 = 200_000

#: 来源的说法（/status 里显示）
SOURCE_CONFIG, SOURCE_API, SOURCE_NAME, SOURCE_DEFAULT = "配置", "接口查询", "按模型名推断", "默认"

_CLAUDE = re.compile(r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d+))?")


def known(model: str) -> int | None:
    """按模型名推断上下文长度；不认识返回 None。

    认中转站常见的写法：大小写、点号（claude-sonnet-4.6）、前缀（anthropic/claude-…）、后缀（-thinking、日期）。
    Claude：Opus / Sonnet 4.6 起、Haiku 5 起、Fable、Mythos 都是 1M；更早的（Opus 4.5、Sonnet 4.5、Haiku 4.5、
    Claude 3.x……）按 200k。名字里带 [1m]（Claude Code 的写法）也按 1M。
    """
    name = model.lower().replace(".", "-")
    if "[1m]" in name:
        return M
    if "claude" not in name:
        return None
    if re.search(r"claude-(fable|mythos)", name):
        return M
    m = _CLAUDE.search(name)
    if m is None:
        return K200  # claude-3-7-sonnet-… 这类旧写法
    family, major = m.group(1), int(m.group(2))
    minor = int(m.group(3)) if m.group(3) and len(m.group(3)) <= 2 else 0  # claude-sonnet-4-20250514：后面是日期
    if family == "haiku":
        return M if major >= 5 else K200
    return M if (major, minor) >= (4, 6) else K200


def fetch(profile: Profile, transport: httpx.BaseTransport | None = None) -> int | None:
    """向服务商查这个模型的上下文长度；查不到（不支持、出错、中转站不转发）返回 None。

    会发网络请求，别在界面线程里调。"""
    base = profile.base_url.rstrip("/")
    try:
        with http_client(timeout=8.0, proxy=profile.proxy, transport=transport) as http:
            if profile.protocol == "anthropic":
                from heng_agent.model.client import ANTHROPIC_VERSION

                resp = http.get(f"{base.removesuffix('/v1')}/v1/models/{profile.model}",
                                headers={"anthropic-version": ANTHROPIC_VERSION, **profile.auth_headers()})  # fmt: skip
                data = resp.json() if resp.status_code < 400 else {}
                return _positive(data.get("max_input_tokens") if isinstance(data, dict) else None)
            resp = http.get(f"{base}/models", headers=profile.auth_headers())
            items = resp.json().get("data", []) if resp.status_code < 400 else []
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id") == profile.model:
            for key in ("context_length", "context_window", "max_input_tokens", "max_context_length"):
                if (found := _positive(item.get(key))) is not None:
                    return found
    return None


def resolve(profile: Profile | None, fetched: int | None, fallback: int) -> tuple[int, str]:
    """(上下文长度, 来源)。fetched：接口查到的（还没查或查不到是 None）；fallback：设置里的 context_window。"""
    if profile is None:
        return fallback, SOURCE_DEFAULT
    if profile.context_window:
        return profile.context_window, SOURCE_CONFIG
    if fetched:
        return fetched, SOURCE_API
    if (guess := known(profile.model)) is not None:
        return guess, SOURCE_NAME
    return fallback, SOURCE_DEFAULT


def _positive(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def window_key(profile: Profile) -> tuple[str, str]:
    """接口查到的结果按（地址, 模型）记：/model、/provider 换了模型就要重新查。"""
    return profile.base_url, profile.model

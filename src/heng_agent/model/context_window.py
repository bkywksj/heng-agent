"""模型的上下文长度从哪来：配置 → 接口查询 → 按模型名推断 → 默认。

以前只看配置档的 context_window，没写就是 128k：中转站上的 claude-sonnet-4-6（实际 1M）在 102k 就开始自动压缩、
状态栏的百分比也偏高。现在：
1. 配置档里写了 context_window：一直用它（推断错了可以手动纠正，/config set profiles.名字.context_window 数字）；
2. 接口查得到：Anthropic 协议问 /v1/models/{模型}（官方返回 max_input_tokens），OpenAI 协议看模型列表里
   有没有带 context_length 之类的字段（OpenRouter 带）；中转站多半查不到，查不到不算错；
3. 按模型名推断：只收录核对过官方文档的（Claude 见 claude-api 技能的模型表；其他家见 VENDOR_RULES 每行的出处）；
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

#: 其他家：(模型名里的模式, 上下文长度)。按顺序取第一个对得上的，具体的写在前面。
#: 数字取自各家官方文档（2026-10-29 核对，出处见每行）；官方写了「最大输入」小于总长度的，用最大输入
#: （自动压缩看的是输入）。官方页面上找不到的型号不写，宁可退回默认。
VENDOR_RULES: list[tuple[re.Pattern[str], int]] = [
    (re.compile(p), n)
    for p, n in [
        # DeepSeek：api-docs.deepseek.com/quick_start/pricing
        (r"deepseek-(v4|flash)", 1_000_000),
        # Kimi：platform.kimi.com/docs/models
        (r"kimi-k3", 1_048_576),
        (r"kimi-k2", 262_144),
        # 智谱：docs.bigmodel.cn/cn/guide/start/model-overview
        (r"glm-5\.[23]|glm-4-long", 1_000_000),
        (r"glm-5|glm-4\.[67]", 200_000),
        (r"glm-4\.5", 128_000),
        # 通义千问：help.aliyun.com/zh/model-studio/text-generation-model/ 与 qwen3-8-max 页
        # （qwen3.8-max 最大输入 983,616（开思考时）；同档其他型号没写最大输入，按同一个数）
        (r"qwen3\.[78]-(max|plus|flash)|qwen-(plus|flash)|qwen3-coder-flash", 983_616),
        (r"qwen-max$", 32_768),
        # OpenAI：developers.openai.com/api/docs/models（gpt-6-astra 最大输入 922K，gpt-5 最大输入 272K）
        (r"gpt-6", 922_000),
        (r"gpt-5", 272_000),
        (r"gpt-4\.1", 1_000_000),
        # Gemini：ai.google.dev/gemini-api/docs/models（输入上限；图片、语音、实时模型不算）
        (r"gemini-(2\.5|3)(?!.*(image|tts|live))", 1_048_576),
        # MiniMax：platform.minimax.cn/docs/guides/models-intro
        (r"minimax-m3", 1_000_000),
    ]
]


def known(model: str) -> int | None:
    """按模型名推断上下文长度；不认识返回 None。

    认中转站常见的写法：大小写、点号（claude-sonnet-4.6）、前缀（anthropic/claude-…）、后缀（-thinking、日期）。
    Claude：Opus / Sonnet 4.6 起、Haiku 5 起、Fable、Mythos 都是 1M；更早的（Opus 4.5、Sonnet 4.5、Haiku 4.5、
    Claude 3.x……）按 200k。名字里带 [1m]（Claude Code 的写法）也按 1M。
    """
    raw = model.lower()
    name = raw.replace(".", "-")
    if "[1m]" in name:
        return M
    if "claude" not in name:
        return next((n for pattern, n in VENDOR_RULES if pattern.search(raw)), None)
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

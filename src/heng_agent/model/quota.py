"""查服务商的额度 / 余额，给状态栏显示。只用配置档里已有的 base_url 与 API key，不要别的凭据。

按 base_url 的域名认是哪家（做法与接口照 tauri-cc 的 cc_quota_statusline.js）：
- DeepSeek、Moonshot / Kimi 开放平台、硅基流动：余额；
- Kimi Code 订阅：5 小时与本周期还剩几次；智谱 GLM Coding Plan：5 小时已用百分比与重置倒计时；
- 其它（多半是中转站）：试 new-api / one-api 的 OpenAI 风格账单接口，限额的显示余额、不限额的显示已用多少
  （实测 yongye 是 new-api：不限额度的 key 返回 hard_limit_usd = 1 亿，total_usage 单位是美分）。
查不到（接口不存在、没权限、网络不通）返回 None，状态栏就不显示这一段，不报错。

做不到的：Claude 订阅的 5h / 7d 额度要 Claude 登录的 OAuth 令牌；OpenRouter 要另一把管理密钥——都没有。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from heng_agent.core.net import http_client

TIMEOUT = 5.0
UNLIMITED_USD = 1e7  # new-api 不限额度时 hard_limit_usd 报 1 亿：超过这个数就当不限额度


@dataclass(frozen=True)
class Quota:
    text: str  # 状态栏上显示的一小段，如「余额 ¥23.5」「5h 43% 4h47m」
    level: str = "ok"  # ok / warn / hot：决定颜色（余额快没了、额度快用完时变色）


def _money(amount: float, currency: str = "CNY") -> str:
    sign = {"CNY": "¥", "USD": "$"}.get(currency.upper(), currency.upper() + " ")
    return f"{sign}{amount:.2f}" if amount < 100 else f"{sign}{amount:.0f}"


def _countdown(reset_at: float | None) -> str:
    """到重置时间还有多久：4h47m、3d2h；没有或已过返回空串。reset_at 是秒级时间戳。"""
    if not reset_at:
        return ""
    left = int(reset_at - time.time())
    if left <= 0:
        return ""
    days, rest = divmod(left, 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    return f" {days}d{hours}h" if days else f" {hours}h{minutes}m"


def _seconds(value: Any) -> float | None:
    """时间戳统一成秒：毫秒级的（大于 1e11）除以 1000；ISO 字符串也认。"""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _balance(amount: float, currency: str = "CNY", *, available: bool = True) -> Quota:
    level = "hot" if not available or amount <= 1 else "warn" if amount < 10 else "ok"
    return Quota(f"余额 {_money(amount, currency)}", level)


def _deepseek(client: httpx.Client, key: str, host: str) -> Quota | None:
    data = client.get("https://api.deepseek.com/user/balance", headers=_bearer(key)).json()
    infos = data.get("balance_infos") or []
    if not infos:
        return None
    first = infos[0]
    return _balance(float(first.get("total_balance") or 0), first.get("currency") or "CNY",
                    available=data.get("is_available", True) is not False)  # fmt: skip


def _moonshot(client: httpx.Client, key: str, host: str) -> Quota | None:
    data = client.get(f"https://{host}/v1/users/me/balance", headers=_bearer(key)).json()
    body = data.get("data") or data
    amount = body.get("available_balance")
    return _balance(float(amount)) if isinstance(amount, (int, float, str)) and str(amount) else None


def _siliconflow(client: httpx.Client, key: str, host: str) -> Quota | None:
    data = client.get(f"https://{host}/v1/user/info", headers=_bearer(key)).json()
    amount = (data.get("data") or {}).get("totalBalance")
    return _balance(float(amount)) if amount is not None else None


def _left(limit: Any, used: Any, remaining: Any) -> tuple[int, int] | None:
    try:
        total = int(limit)
    except (TypeError, ValueError):
        return None
    left = int(remaining) if isinstance(remaining, (int, float, str)) and str(remaining) else total - int(used or 0)
    return left, total


def _kimi_code(client: httpx.Client, key: str, host: str) -> Quota | None:
    data = client.get("https://api.kimi.com/coding/v1/usages", headers=_bearer(key)).json()
    parts, worst = [], 1.0
    for item in data.get("limits") or []:
        window = item.get("window") or {}
        unit = str(window.get("timeUnit", "")).upper()
        five_hours = (window.get("duration") == 300 and "MINUTE" in unit) or (
            window.get("duration") == 5 and "HOUR" in unit
        )
        detail = item.get("detail") or {}
        pair = _left(detail.get("limit"), detail.get("used"), detail.get("remaining")) if five_hours else None
        if pair:
            parts.append(f"5h 剩{pair[0]}/{pair[1]}{_countdown(_seconds(detail.get('resetTime')))}")
            worst = min(worst, pair[0] / pair[1] if pair[1] else 1)
            break
    usage = data.get("usage") or {}
    pair = _left(usage.get("limit"), usage.get("used"), usage.get("remaining"))
    if pair:
        parts.append(f"周期 剩{pair[0]}/{pair[1]}")
        worst = min(worst, pair[0] / pair[1] if pair[1] else 1)
    if not parts:
        return None
    return Quota(" · ".join(parts), "hot" if worst < 0.1 else "warn" if worst < 0.3 else "ok")


def _zhipu(client: httpx.Client, key: str, host: str) -> Quota | None:
    headers = {**_bearer(key), "User-Agent": "ZhiPu-Monitor/1.0"}
    data = client.get("https://bigmodel.cn/api/monitor/usage/quota/limit", headers=headers).json()
    if data.get("code") != 200:
        return None
    for item in (data.get("data") or {}).get("limits") or []:
        if item.get("type") == "TOKENS_LIMIT" and item.get("unit") == 3:  # unit 3 = 5 小时窗口
            pct = float(item.get("percentage") or 0)
            level = "hot" if pct >= 90 else "warn" if pct >= 70 else "ok"
            return Quota(f"5h {pct:.0f}%{_countdown(_seconds(item.get('nextResetTime')))}", level)
    return None


def _relay(client: httpx.Client, key: str, base_url: str) -> Quota | None:
    """new-api / one-api 一类中转站的 OpenAI 风格账单接口：总额度（美元）与已用（美分）。"""
    root = base_url.rstrip("/").removesuffix("/v1")
    sub = client.get(f"{root}/v1/dashboard/billing/subscription", headers=_bearer(key))
    if sub.status_code != 200 or "json" not in sub.headers.get("content-type", ""):
        return None
    limit = sub.json().get("hard_limit_usd")
    usage = client.get(f"{root}/v1/dashboard/billing/usage", headers=_bearer(key))
    if usage.status_code != 200 or "json" not in usage.headers.get("content-type", ""):
        return None
    used_cents = usage.json().get("total_usage")
    if not isinstance(limit, (int, float)) or not isinstance(used_cents, (int, float)):
        return None
    used = used_cents / 100
    if limit >= UNLIMITED_USD:  # 不限额度：只能看已经用了多少
        return Quota(f"已用 {_money(used, 'USD')}")
    return _balance(max(limit - used, 0.0), "USD")


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


VENDORS = {
    "api.deepseek.com": _deepseek,
    "api.moonshot.cn": _moonshot, "api.moonshot.ai": _moonshot, "api.kimi.ai": _moonshot,
    "api.siliconflow.cn": _siliconflow, "api.siliconflow.com": _siliconflow,
    "api.kimi.com": _kimi_code,
    "open.bigmodel.cn": _zhipu, "api.z.ai": _zhipu,
}  # fmt: skip
#: 认得出、但没有用 API key 就能查的额度接口：不去试中转站的账单接口（免得白发请求）
NO_QUOTA = ("api.anthropic.com", "openrouter.ai", "dashscope.aliyuncs.com", "api.openai.com")
LOCAL = ("localhost", "127.0.0.1", "::1")


def fetch(base_url: str, key: str | None, *, proxy: str | None = None) -> Quota | None:
    """查一次额度；查不到返回 None（不抛异常：状态栏是锦上添花）。会发网络请求，别在界面线程里调。"""
    if not key:
        return None
    host = (urlparse(base_url).hostname or "").lower()
    if not host or host in LOCAL or host in NO_QUOTA:
        return None  # 本机的 heng serve、Ollama 没有额度一说
    vendor = VENDORS.get(host) or (_zhipu if host.endswith(".bigmodel.cn") else None)
    try:
        with http_client(proxy=proxy, timeout=TIMEOUT) as client:
            if vendor is not None:
                return vendor(client, key, host)
            return _relay(client, key, base_url)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None

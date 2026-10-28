"""服务商额度：按域名认是哪家、调对的接口、把返回换算成状态栏上的一小段；查不到就不显示、不报错。"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from heng_agent.model import quota
from heng_agent.ui.prompt import toolbar


def _serve(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> list[httpx.Request]:
    """把 quota 用的 http_client 换成假的：按完整网址回 JSON，没登记的回 404。返回收到的请求。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url not in routes:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=routes[url])

    monkeypatch.setattr(quota, "http_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def test_new_api_relay_unlimited_shows_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """实测 yongye（new-api）：不限额度的 key 返回 hard_limit_usd = 1 亿，total_usage 单位是美分。"""
    seen = _serve(monkeypatch, {
        "https://cc.example.cn/v1/dashboard/billing/subscription": {"hard_limit_usd": 100000000},
        "https://cc.example.cn/v1/dashboard/billing/usage": {"total_usage": 7197.9736},
    })  # fmt: skip
    found = quota.fetch("https://cc.example.cn", "sk-x")
    assert found == quota.Quota("已用 $71.98")
    assert seen[0].headers["authorization"] == "Bearer sk-x"


def test_new_api_relay_limited_shows_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {
        "https://relay.example.com/v1/dashboard/billing/subscription": {"hard_limit_usd": 20},
        "https://relay.example.com/v1/dashboard/billing/usage": {"total_usage": 1950},  # 美分
    })  # fmt: skip
    assert quota.fetch("https://relay.example.com/v1", "k") == quota.Quota("余额 $0.50", "hot")


def test_deepseek_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {"https://api.deepseek.com/user/balance": {
        "is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "23.45"}]}})  # fmt: skip
    assert quota.fetch("https://api.deepseek.com/anthropic", "k") == quota.Quota("余额 ¥23.45")


def test_zhipu_five_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_ms = (time.time() + 2 * 3600 + 5 * 60) * 1000
    _serve(monkeypatch, {"https://bigmodel.cn/api/monitor/usage/quota/limit": {"code": 200, "data": {"limits": [
        {"type": "TIME_LIMIT", "unit": 5, "percentage": 1},
        {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 75, "nextResetTime": reset_ms}]}}})  # fmt: skip
    found = quota.fetch("https://open.bigmodel.cn/api/anthropic", "k")
    assert found is not None and found.text.startswith("5h 75% 2h") and found.level == "warn"


def test_kimi_code_left(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {"https://api.kimi.com/coding/v1/usages": {
        "usage": {"limit": 1000, "used": 100, "remaining": 900},
        "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {"limit": 100, "used": 95, "remaining": 5}}]}})  # fmt: skip
    found = quota.fetch("https://api.kimi.com/coding", "k")
    assert found is not None and found.text == "5h 剩5/100 · 周期 剩900/1000" and found.level == "hot"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765", "https://api.anthropic.com", "https://openrouter.ai/api"])
def test_no_quota_hosts_send_nothing(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """本机服务没有额度；Anthropic、OpenRouter 要别的凭据：都不发请求。"""
    seen = _serve(monkeypatch, {})
    assert quota.fetch(url, "k") is None and seen == []


def test_unknown_relay_without_billing_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {})  # 都 404
    assert quota.fetch("https://unknown.example.com", "k") is None


def test_network_error_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连不上")

    monkeypatch.setattr(quota, "http_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(boom)))
    assert quota.fetch("https://api.deepseek.com", "k") is None


def test_no_key_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, {})
    assert quota.fetch("https://api.deepseek.com", None) is None and seen == []


def test_quota_in_toolbar_and_dropped_first() -> None:
    def line(width: int | None) -> str:
        bar = toolbar("ask", "yongye · claude-sonnet-5", "/w/heng-agent", 1000, 200_000, speed=40, width=width,
                      quota=("class:bar.quota", "已用 $72"))  # fmt: skip
        return "".join(t for _, t in bar).split("\n")[0]

    assert "已用 $72" in line(None) and "40t/s" in line(None)
    narrow = line(len(line(None)) - 2)  # 放不下：额度最先省掉
    assert "已用 $72" not in narrow and "40t/s" in narrow


def test_payload_is_json_serializable() -> None:
    json.dumps(quota.Quota("x").__dict__)

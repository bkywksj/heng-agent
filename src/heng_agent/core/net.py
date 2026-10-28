"""建 HTTP 客户端的统一入口：连本机地址时不走环境变量里的代理。

httpx 默认按 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 走代理，连 127.0.0.1、localhost 也不例外。
开着代理软件的电脑上，本机的模型服务（heng serve、Ollama）和本机的 MCP 服务都会被转给代理：
服务没起来时报的是代理回的「503」而不是「连不上」；代理不在本机（公司代理）时，「localhost」变成了代理自己，
请求必然失败。所以本机地址一律直连，其余照旧尊重代理设置。

代理设置（settings.proxy、服务商配置档的 proxy）可以盖过环境变量：指定代理地址，或 direct 不走代理。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

import httpx

#: 直连的本机地址（任意端口）。httpx 按「越具体越优先」挑路由，这几条压过代理那条通配
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")
#: 代理设置写这个 = 不走代理，环境变量里设了也不用
DIRECT = "direct"
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def check_proxy(value: str | None) -> str | None:
    """校验代理设置（settings.proxy、配置档的 proxy）：不设 / 空串 = 按环境变量；direct = 不走代理；其余要是代理地址。

    写错在读配置时就报出来，不等到发请求才莫名其妙地连不上。
    """
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value == DIRECT:
        return value
    scheme = value.split("://", 1)[0].lower() if "://" in value else ""
    if scheme not in PROXY_SCHEMES:
        raise ValueError(f"代理要写成 http://主机:端口、socks5://主机:端口，或 {DIRECT}（不走代理），现在是「{value}」")
    return value


def mask_proxy(url: str) -> str:
    """代理地址里的密码换成 ***（http://用户:密码@主机:端口），给 /doctor 显示用，不把密码打到屏幕上。"""
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url)


def describe_proxy(proxy: str | None, environ: Mapping[str, str] | None = None) -> str:
    """一句话说清请求实际走哪个代理（/doctor 用）：配置写的、按环境变量的、还是直连。"""
    if proxy == DIRECT:
        return "不走代理（配置里写了 direct）"
    if proxy:
        return f"{mask_proxy(proxy)}（配置）"
    env = os.environ if environ is None else environ
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        if env.get(name):
            return f"{mask_proxy(env[name])}（环境变量 {name}）"
    return "不走代理（没配置，环境变量也没设）"


def http_client(
    *, proxy: str | None = None, transport: httpx.BaseTransport | None = None, **kwargs: Any
) -> httpx.Client:
    """建 httpx.Client，其余参数同 httpx.Client；本机地址始终不走代理。

    proxy：None = 按 HTTP_PROXY 等环境变量；direct = 不走代理；代理地址 = 用它（环境变量就不管了）。
    传了 transport（测试替身）时原样使用、不加任何路由：否则请求会绕过替身，真的往外连。
    """
    if transport is not None:
        return httpx.Client(transport=transport, **kwargs)
    mounts: dict[str, httpx.BaseTransport | None] = {f"all://{h}": httpx.HTTPTransport() for h in LOOPBACK_HOSTS}
    if proxy == DIRECT:
        # 盖掉环境代理的全部路由：HTTP_PROXY / HTTPS_PROXY 登记成 http:// 与 https://，比 all:// 更具体，
        # 只盖 all:// 的话它们照样生效（测试抓到过）
        for pattern in ("all://", "http://", "https://"):
            mounts[pattern] = httpx.HTTPTransport()
    elif proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(mounts=mounts, **kwargs)

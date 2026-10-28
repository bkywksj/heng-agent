"""建 HTTP 客户端的统一入口：连本机地址时不走环境变量里的代理。

httpx 默认按 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 走代理，连 127.0.0.1、localhost 也不例外。
开着代理软件的电脑上，本机的模型服务（heng serve、Ollama）和本机的 MCP 服务都会被转给代理：
服务没起来时报的是代理回的「503」而不是「连不上」；代理不在本机（公司代理）时，「localhost」变成了代理自己，
请求必然失败。所以本机地址一律直连，其余照旧尊重代理设置。
"""

from __future__ import annotations

from typing import Any

import httpx

#: 直连的本机地址（任意端口）。httpx 按「越具体越优先」挑路由，这几条压过环境代理那条通配
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def http_client(*, transport: httpx.BaseTransport | None = None, **kwargs: Any) -> httpx.Client:
    """建 httpx.Client，参数同 httpx.Client；本机地址不走代理。

    传了 transport（测试替身）时原样使用、不加直连规则：否则本机地址的请求会绕过替身，真的往外连。
    """
    mounts = None if transport is not None else {f"all://{host}": httpx.HTTPTransport() for host in LOOPBACK_HOSTS}
    return httpx.Client(transport=transport, mounts=mounts, **kwargs)

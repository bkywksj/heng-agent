"""建 HTTP 客户端：本机地址不走环境变量里的代理（开着代理软件时，连本机 MCP、heng serve、Ollama 都被转给代理）。"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from heng_agent.core.net import http_client
from heng_agent.mcp import MCPManager, ServerConfig


class _Proxy503(http.server.BaseHTTPRequestHandler):
    """假代理：什么请求都回 503，和常见代理软件连不上目标时一样。

    不能用「没人在听的端口」当代理：那样旧代码也是连接失败，报错和正确行为分不出来（实测这样写的用例在修复前照样通过）。
    """

    def _reply(self) -> None:
        self.send_response(503)
        self.send_header("content-length", "0")
        self.end_headers()

    do_GET = do_POST = do_DELETE = _reply

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def proxied(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """模拟开着代理的电脑：大小写两套代理变量都指向假代理，并清掉 NO_PROXY（本机实测环境里就是小写的 all_proxy）。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Proxy503)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy = f"http://127.0.0.1:{server.server_address[1]}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy)
        monkeypatch.setenv(name.lower(), proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield
    server.shutdown()
    server.server_close()


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def local_port() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.mark.usefixtures("proxied")
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_loopback_bypasses_env_proxy(local_port: int, host: str) -> None:
    with http_client(timeout=5.0) as client:
        resp = client.get(f"http://{host}:{local_port}/")
    assert resp.status_code == 200 and resp.text == "ok"  # 走了代理拿到的是 503


@pytest.mark.usefixtures("proxied")
def test_remote_still_uses_env_proxy() -> None:
    with http_client(timeout=5.0) as client:
        remote = client._transport_for_url(httpx.URL("https://pypi.org/"))
        local = client._transport_for_url(httpx.URL("http://127.0.0.1:8000/"))
    assert remote is not client._transport  # 外部地址照旧交给代理那条路由
    assert local is not remote


@pytest.mark.usefixtures("proxied")
def test_given_transport_is_used_as_is() -> None:
    """测试替身优先：本机地址也交给它，不能偷偷真的往外连。"""
    mock = httpx.MockTransport(lambda request: httpx.Response(200, text=f"mock {request.url.host}"))
    with http_client(transport=mock) as client:
        assert client.get("http://127.0.0.1:5/").text == "mock 127.0.0.1"


@pytest.mark.usefixtures("proxied")
def test_mcp_unreachable_behind_proxy_says_unreachable(tmp_path: Path) -> None:
    """以前开着代理时，本机 MCP 没起来报的是代理回的「返回 503」，看不出是服务没开。"""
    manager = MCPManager([ServerConfig("gone", url="http://127.0.0.1:9/mcp", timeout=3)], tmp_path)
    assert manager.status[0][:2] == ("gone", False) and "连不上" in manager.status[0][2]

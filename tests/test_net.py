"""建 HTTP 客户端：本机地址不走环境变量里的代理（开着代理软件时，连本机 MCP、heng serve、Ollama 都被转给代理）。"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from heng_agent.configfile import settings_overrides
from heng_agent.core.errors import AgentError
from heng_agent.core.net import check_proxy, describe_proxy, http_client
from heng_agent.mcp import MCPManager, ServerConfig
from heng_agent.model.providers import load_profiles


class _Proxy503(http.server.BaseHTTPRequestHandler):
    """假代理：什么请求都回 503，和常见代理软件连不上目标时一样。

    不能用「没人在听的端口」当代理：那样旧代码也是连接失败，报错和正确行为分不出来（实测这样写的用例在修复前照样通过）。
    """

    def _reply(self) -> None:
        # 先把请求体读完再回：不读就回 503 并关连接，带请求体的 POST（MCP）偶尔撞上连接被重置（ReadError），
        # 测试时好时坏（实测连跑 5 次挂 3 次）
        self.rfile.read(int(self.headers.get("content-length") or 0))
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


# ---------- 自定义代理：settings.proxy 与服务商配置档的 proxy ----------


@pytest.fixture
def fake_proxy(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """一个固定回 503 的假代理（返回它的地址）；环境变量里的代理全部清掉，确认用的是设置给的。"""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Proxy503)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("  ", None), ("direct", "direct"), ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
     ("socks5://u:p@10.0.0.2:1080", "socks5://u:p@10.0.0.2:1080")],
)  # fmt: skip
def test_check_proxy_accepts(value: str | None, expected: str | None) -> None:
    assert check_proxy(value) == expected


@pytest.mark.parametrize("value", ["127.0.0.1:7890", "ftp://h:21", "off"])
def test_check_proxy_rejects(value: str) -> None:
    with pytest.raises(ValueError, match="代理要写成"):
        check_proxy(value)  # 漏写 http:// 是最常见的写错，读配置时就要报出来


def test_explicit_proxy_is_used_but_loopback_stays_direct(fake_proxy: str, local_port: int) -> None:
    with http_client(timeout=5.0, proxy=fake_proxy) as client:
        assert client.get("http://example.invalid/").status_code == 503  # 外部地址走了设置的代理
        assert client.get(f"http://127.0.0.1:{local_port}/").text == "ok"  # 本机地址照样直连


@pytest.mark.usefixtures("proxied")
def test_direct_overrides_env_proxy() -> None:
    """环境变量设了代理、配置写 direct：外部地址也不走代理。"""
    with http_client(timeout=5.0) as by_env, http_client(timeout=5.0, proxy="direct") as direct:
        assert isinstance(by_env._transport_for_url(httpx.URL("https://pypi.org/"))._pool, httpcore.HTTPProxy)  # type: ignore[attr-defined]
        assert not isinstance(direct._transport_for_url(httpx.URL("https://pypi.org/"))._pool, httpcore.HTTPProxy)  # type: ignore[attr-defined]


def test_profiles_take_settings_proxy_unless_they_have_their_own(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "settings": {"proxy": "http://10.0.0.1:7890"},
        "profiles": {"cn": {"preset": "deepseek", "proxy": "direct"}, "far": {"preset": "openrouter"}},
    }), encoding="utf-8")  # fmt: skip
    profiles, _ = load_profiles(home)
    assert profiles["far"].proxy == "http://10.0.0.1:7890"  # 没写的用 settings.proxy
    assert profiles["deepseek"].proxy == "http://10.0.0.1:7890"  # 内置预设也一样
    assert profiles["cn"].proxy == "direct"  # 配置档自己写的优先


def test_profiles_without_any_proxy_setting_follow_env(tmp_path: Path) -> None:
    profiles, _ = load_profiles(tmp_path / "nothing")
    assert all(p.proxy is None for p in profiles.values())  # None = 按环境变量，和以前一样


@pytest.mark.parametrize(
    "project",
    [
        {"settings": {"proxy": "http://evil:8080"}},
        {"profiles": {"x": {"preset": "deepseek", "proxy": "http://evil:8080"}}},
    ],
)
def test_project_config_cannot_set_proxy(tmp_path: Path, project: dict[str, Any]) -> None:
    """项目配置跟着仓库走：别人的仓库不能借它把你的模型请求（连同密钥）转到他的代理上。"""
    work = tmp_path / "w"
    (work / ".heng-agent").mkdir(parents=True)
    (work / ".heng-agent" / "config.json").write_text(json.dumps(project), encoding="utf-8")
    with pytest.raises(AgentError, match="代理写在用户目录的配置里"):
        load_profiles(tmp_path / "h", work)
    if "settings" in project:
        assert "proxy" not in settings_overrides(tmp_path / "h", work)  # MCP、web_fetch 也不受它影响


def test_bad_settings_proxy_is_reported(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"settings": {"proxy": "127.0.0.1:7890"}}), encoding="utf-8")
    with pytest.raises(AgentError, match=r"settings.proxy 有误"):
        load_profiles(home)


def test_describe_proxy_masks_password() -> None:
    assert describe_proxy("http://bob:s3cret@10.0.0.1:8080") == "http://bob:***@10.0.0.1:8080（配置）"
    assert describe_proxy("direct").startswith("不走代理")
    assert describe_proxy(None, {"HTTPS_PROXY": "http://a:b@p:1"}) == "http://a:***@p:1（环境变量 HTTPS_PROXY）"
    assert describe_proxy(None, {}).startswith("不走代理")


def test_web_fetch_uses_settings_proxy(fake_proxy: str, tmp_path: Path) -> None:
    from heng_agent.tools.base import ToolContext
    from heng_agent.tools.web import WebFetch

    result = WebFetch().run({"url": "http://example.invalid/"}, ToolContext(workdir=tmp_path, proxy=fake_proxy))
    assert result.is_error and result.content.startswith("HTTP 503")  # 回 503 的是假代理：确实走了它


def test_http_mcp_uses_settings_proxy(fake_proxy: str, tmp_path: Path) -> None:
    manager = MCPManager([ServerConfig("far", url="http://example.invalid/mcp", timeout=3)], tmp_path, proxy=fake_proxy)
    assert "返回 503" in manager.status[0][2]  # 回 503 的是假代理：远端 MCP 服务的请求走了它

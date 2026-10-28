"""MCP 客户端（stdio）：启动外部工具服务，把它提供的工具接进智能体。

配置写在 config.json（用户 ~/.heng-agent/ 与项目 .heng-agent/ 都可以）：

    "mcp_servers": {
      "sigil": {"command": "sigil-mcp", "args": ["--stdio"], "env": {"FOO": "bar"}, "timeout": 60}
    }

协议：JSON-RPC 2.0，每条消息一行 JSON，走子进程的标准输入输出。握手 initialize → notifications/initialized，
再 tools/list 取工具清单（支持分页），调用走 tools/call。

工具名注册成 `mcp__服务名__工具名`（只留字母数字下划线连字符，≤ 64 字符），不和内置工具撞名。
权限类别 mcp：外部工具可能写数据库、发消息，除 auto 外都要用户确认。
**项目里配置的 MCP 服务等于「启动一个程序」**，与钩子同样可能来自别人的仓库，必须用户确认信任才启动
（信任按配置内容指纹，一改就要重新确认）。退出时按 PID 杀掉每个服务的整棵进程树。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from heng_agent import __version__
from heng_agent.core import trust as trust_store
from heng_agent.core.errors import AgentError
from heng_agent.core.net import http_client
from heng_agent.core.types import ToolResult
from heng_agent.tools.base import Tool, ToolContext
from heng_agent.tools.shell import IS_WINDOWS, kill_tree

PROTOCOL_VERSION = "2025-06-18"
MAX_RESULT_CHARS = 30_000


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str = ""  # stdio：启动哪个程序
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    source: str = "user"
    url: str = ""  # http：连已经在跑的服务（Streamable HTTP）
    headers: dict[str, str] = field(default_factory=dict)  # http 请求头，如鉴权令牌

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: str) -> str:
    """${变量} 与 ${变量:-默认值}（Claude Code 的 .mcp.json 写法）：团队常用它把令牌放在环境变量里、不提交进仓库。
    没设也没默认值的留空。"""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)


def _read_servers(path: Path, source: str) -> list[ServerConfig]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} 不是合法 JSON：{exc}") from exc
    servers = []
    # 也认 mcpServers（Claude Code、Kimi 的写法），它们的配置可以直接贴过来
    entries = {**(data.get("mcpServers") or {}), **(data.get("mcp_servers") or {})}
    for name, cfg in entries.items():
        try:
            url = str(cfg.get("url") or "")
            command = str(cfg.get("command") or "")
            if not url and not command:
                raise KeyError("command 或 url")
            if str(cfg.get("type", "")).lower() == "sse":
                raise ValueError("老式 SSE 传输不支持，请用服务的 Streamable HTTP 地址（通常以 /mcp 结尾）")
            servers.append(ServerConfig(
                name, _expand(command), [_expand(str(a)) for a in cfg.get("args", [])],
                {str(k): _expand(str(v)) for k, v in (cfg.get("env") or {}).items()},
                float(cfg.get("timeout", 60)), source, _expand(url),
                {str(k): _expand(str(v)) for k, v in (cfg.get("headers") or {}).items()},
            ))  # fmt: skip
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise AgentError(f"{path} 的 MCP 服务 {name} 写错了（要有 command 或 url）：{exc}") from exc
    return servers


def load_servers(home: Path, workdir: Path) -> tuple[list[ServerConfig], list[ServerConfig]]:
    """返回 (用户配置的服务, 项目配置的服务)；同名时项目的覆盖用户的。

    项目的也读 Claude Code 的 .mcp.json（heng-agent 自己的配置同名时优先）。项目里的服务都要用户确认信任才启动。
    """
    user = _read_servers(home / "config.json", "user")
    claude_project = _read_servers(workdir / ".mcp.json", "project")
    heng_project = _read_servers(workdir / ".heng-agent" / "config.json", "project")
    heng_names = {s.name for s in heng_project}
    project = [s for s in claude_project if s.name not in heng_names] + heng_project
    project_names = {s.name for s in project}
    return [s for s in user if s.name not in project_names], project


def fingerprint(servers: list[ServerConfig]) -> str:
    # url 与请求头也算进去：项目配置里把 url 换成别的服务，信任要失效、重新问
    raw = json.dumps([(s.name, s.command, s.args, sorted(s.env.items()), s.url, sorted(s.headers.items()))
                      for s in servers], ensure_ascii=False)  # fmt: skip
    return "mcp:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _trust_key(workdir: Path) -> str:
    return f"{workdir.resolve()}#mcp"


def is_trusted(home: Path, workdir: Path, servers: list[ServerConfig]) -> bool:
    return not servers or trust_store.is_trusted(home, _trust_key(workdir), fingerprint(servers))


def trust(home: Path, workdir: Path, servers: list[ServerConfig]) -> None:
    trust_store.trust(home, _trust_key(workdir), fingerprint(servers))


class _Session:
    """stdio 与 http 共用的握手：initialize → notifications/initialized → tools/list（支持分页）。"""

    config: ServerConfig

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str) -> None:
        raise NotImplementedError

    def on_initialized(self, result: dict[str, Any]) -> None:
        """握手应答到了（http 要记下协商出的协议版本，后续请求带上）。"""

    def initialize(self) -> list[dict[str, Any]]:
        """握手并取工具清单。"""
        result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                             "clientInfo": {"name": "heng-agent", "version": __version__}})  # fmt: skip
        self.on_initialized(result)
        self.notify("notifications/initialized")
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += page.get("tools", [])
            cursor = page.get("nextCursor")
            if not cursor:
                return tools

    def close(self) -> None:
        raise NotImplementedError


class MCPClient(_Session):
    """一个 MCP 服务的连接：子进程 + 读线程。请求按 id 配对响应，通知忽略。"""

    def __init__(self, config: ServerConfig, workdir: Path) -> None:
        self.config = config
        extra: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
        )
        try:
            self.proc = subprocess.Popen(
                [config.command, *config.args],
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**os.environ, **config.env},
                **extra,
            )
        except OSError as exc:
            raise AgentError(f"启动 MCP 服务 {config.name} 失败：{exc}") from exc
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue  # 服务往标准输出打了日志之类的非协议内容：跳过
            box = self._pending.get(msg.get("id")) if isinstance(msg, dict) else None
            if box is not None:
                box.put(msg)
        for box in list(self._pending.values()):  # 进程退出：唤醒所有还在等的请求
            box.put({"error": {"message": "MCP 服务进程已退出"}})

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as exc:
            raise AgentError(f"MCP 服务 {self.config.name} 已断开：{exc}") from exc

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            box: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[rid] = box
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            try:
                msg = box.get(timeout=self.config.timeout)
            except queue.Empty as exc:
                raise AgentError(f"MCP 服务 {self.config.name} 的 {method} 超时（{self.config.timeout:g} 秒）") from exc
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            raise AgentError(f"MCP 服务 {self.config.name} 报错：{msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        if self.proc.stdin is not None:
            with contextlib.suppress(OSError):  # 服务已经退出时关管道会报错，无妨
                self.proc.stdin.close()
        kill_tree(self.proc)  # type: ignore[arg-type]


class HttpMCPClient(_Session):
    """Streamable HTTP：每条 JSON-RPC 消息 POST 到服务地址；服务可以直接回 JSON，也可以回一段事件流
    （text/event-stream，里面有对应 id 的响应），两种都认。握手应答头里的 Mcp-Session-Id 之后每次请求都带上，
    关闭时发 DELETE 结束会话。对方本来就在跑，不归我们启停。
    """

    def __init__(self, config: ServerConfig, workdir: Path) -> None:
        self.config = config
        self.http = http_client(timeout=httpx.Timeout(config.timeout, connect=10.0))
        self.session_id: str | None = None
        self.protocol: str | None = None
        self._next_id = 0
        self._lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   **self.config.headers}  # fmt: skip
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    def _post(self, payload: dict[str, Any], expect_id: int | None) -> dict[str, Any]:
        name = self.config.name
        try:
            with self.http.stream("POST", self.config.url, json=payload, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise AgentError(f"MCP 服务 {name} 返回 {resp.status_code}：{resp.text[:200]}")
                if resp.headers.get("mcp-session-id"):
                    self.session_id = resp.headers["mcp-session-id"]
                if expect_id is None:  # 通知：服务回 202，没有内容
                    return {}
                if "text/event-stream" in resp.headers.get("content-type", ""):
                    for msg in _sse_messages(resp.iter_lines()):
                        if msg.get("id") == expect_id:
                            return msg
                    raise AgentError(f"MCP 服务 {name} 的事件流结束了，没收到响应")
                resp.read()
                return resp.json()
        except httpx.HTTPError as exc:
            raise AgentError(f"连不上 MCP 服务 {name}（{self.config.url}）：{type(exc).__name__}") from exc
        except ValueError as exc:
            raise AgentError(f"MCP 服务 {name} 返回的不是 JSON") from exc

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        msg = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, rid)
        if "error" in msg:
            raise AgentError(f"MCP 服务 {self.config.name} 报错：{msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method}, None)

    def on_initialized(self, result: dict[str, Any]) -> None:
        self.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)

    def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(httpx.HTTPError):  # 结束会话是礼貌，对方不支持或已断开都无妨
                self.http.delete(self.config.url, headers=self._headers(), timeout=3.0)
        self.http.close()


def _sse_messages(lines: Any) -> Any:
    """事件流里的 JSON-RPC 消息：data: 行累积到空行为一条。逐行处理、边读边交出——
    不能先把整个流读完：服务发完响应后不一定马上关连接，那样会一直等下去。"""
    data: list[str] = []

    def flush() -> dict[str, Any] | None:
        text = "\n".join(data)
        data.clear()
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return None
        return msg if isinstance(msg, dict) else None

    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            msg = flush()
            if msg is not None:
                yield msg
    if data:
        msg = flush()
        if msg is not None:
            yield msg


def _tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]


def render_content(result: dict[str, Any]) -> str:
    parts = []
    for item in result.get("content", []):
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(f"[{item.get('type', '未知')} 内容，heng-agent 暂不展示]")
    text = "\n".join(parts) or "（没有返回内容）"
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n…（太长，后面截掉了）"


class MCPTool(Tool):
    kind = "mcp"

    def __init__(self, client: _Session, spec: dict[str, Any]) -> None:
        self.client = client
        self.remote_name = str(spec["name"])
        self.name = _tool_name(client.config.name, self.remote_name)  # type: ignore[misc]
        self.description = f"[MCP {client.config.name}] {spec.get('description', '')}".strip()  # type: ignore[misc]
        self.input_schema = spec.get("inputSchema") or {"type": "object", "properties": {}}  # type: ignore[misc]

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = self.client.request("tools/call", {"name": self.remote_name, "arguments": args})
        except AgentError as exc:
            # 服务进程退出、连接断开、超时……：告诉模型（再由它转告用户）可以重连，不用重启 heng-agent
            hint = f"如果这个服务已经断开，请用户执行 /mcp reconnect {self.client.config.name} 重新连接后再试。"
            return ToolResult(f"{exc}\n{hint}", is_error=True)
        return ToolResult(render_content(result), is_error=bool(result.get("isError")))


class MCPManager:
    """启动一组 MCP 服务、收集它们的工具；启动失败的服务跳过并记下原因，不影响其他服务。

    单个服务可以断开（/mcp disable）、重连（/mcp reconnect、enable）：崩了或刚更新过的服务不用重启 heng-agent。
    """

    def __init__(self, servers: list[ServerConfig], workdir: Path) -> None:
        self.workdir = workdir
        self.configs = {s.name: s for s in servers}  # 重连要用原配置
        self.clients: dict[str, _Session] = {}  # 服务名 → 连接
        self.tools: list[MCPTool] = []
        self.errors: list[str] = []
        #: 每个服务的连接状态：(名字, 是否连上, 工具数与连接方式或失败原因)，欢迎页逐行显示
        self.status: list[tuple[str, bool, str]] = []
        # 各服务同时连：启动慢的服务（npx 拉起的要好几秒）以前一个接一个排队，6 个服务实测 10 秒，并行后只等最慢的那个。
        # 线程里只建连接、握手；登记工具与状态回到这里按配置顺序做，工具与欢迎页的顺序不随谁先连上而变
        with ThreadPoolExecutor(max_workers=max(1, len(servers)), thread_name_prefix="mcp-connect") as pool:
            opened = list(pool.map(self._open, servers))
        for config, result in zip(servers, opened, strict=True):
            self._register(config, result)

    def _set_status(self, name: str, ok: bool, detail: str) -> None:
        """同名的状态原地替换（重连后位置不变），没有就追加。"""
        entry = (name, ok, detail)
        for i, (old, _, _) in enumerate(self.status):
            if old == name:
                self.status[i] = entry
                return
        self.status.append(entry)

    def _open(self, config: ServerConfig) -> tuple[_Session, list[dict[str, Any]]] | AgentError:
        """建连接并握手，返回 (连接, 工具定义) 或失败原因。不碰共享状态，可以在线程里并行跑。"""
        client: _Session | None = None  # 失败时只关这一个，不能误关别的服务
        try:
            client = HttpMCPClient(config, self.workdir) if config.url else MCPClient(config, self.workdir)
            return client, client.initialize()
        except AgentError as exc:
            if client is not None:
                client.close()
            return exc

    def _connect(self, config: ServerConfig) -> list[MCPTool]:
        return self._register(config, self._open(config))

    def _register(
        self, config: ServerConfig, result: tuple[_Session, list[dict[str, Any]]] | AgentError
    ) -> list[MCPTool]:
        """把一次连接的结果记进工具列表与状态（只在主线程里调）。"""
        if isinstance(result, AgentError):
            self.errors.append(str(result))
            self._set_status(config.name, False, str(result))
            return []
        client, specs = result
        self.clients[config.name] = client
        tools = [MCPTool(client, spec) for spec in specs]
        self.tools += tools
        self._set_status(config.name, True, f"{len(specs)} tools ({config.transport})")
        return tools

    def has(self, name: str) -> bool:
        return name in self.configs

    def disconnect(self, name: str) -> list[str]:
        """断开一个服务（stdio 连进程一起关），返回被移除的工具名。"""
        client = self.clients.pop(name, None)
        if client is not None:
            client.close()
        removed = [t.name for t in self.tools if t.client.config.name == name]
        self.tools = [t for t in self.tools if t.client.config.name != name]
        return removed

    def reconnect(self, name: str) -> tuple[list[str], list[MCPTool]]:
        """断开后按原配置重新连：返回 (旧工具名, 新工具)。连不上时新工具为空，原因记在 status 里。"""
        removed = self.disconnect(name)
        return removed, self._connect(self.configs[name])

    def close(self) -> None:
        # 各服务同时收尾：Windows 上结束一个 stdio 服务要起一次 taskkill（约 0.5～1 秒），
        # 以前排队关，6 个服务退出要等 5 秒
        clients = list(self.clients.values())
        self.clients.clear()  # 可以重复调用：界面和命令行入口退出时都会关
        if clients:
            with ThreadPoolExecutor(max_workers=len(clients), thread_name_prefix="mcp-close") as pool:
                list(pool.map(lambda client: client.close(), clients))

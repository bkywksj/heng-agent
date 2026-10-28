"""ACP（Agent Client Protocol）模式：heng-agent --acp。

让编辑器 / 桌面应用（智码、Zed、JetBrains……）通过标准输入输出、用 JSON-RPC 驱动 heng-agent：
新建 / 恢复 / 列出会话，发消息，流式收到文字、思考、工具调用与计划，权限确认由客户端弹窗，随时取消。
协议相关的代码都在这一个模块里：官方 SDK（agent-client-protocol，0.x）升级时只改这里。

几个要点：
- 核心循环（Agent.run）是同步的，放在另一个线程跑；主线程跑异步的协议收发。中断靠取消标志（见 Agent.cancel）。
- 发给客户端的一切（文字、工具调用、权限确认请求）都进同一个队列、按顺序发：否则权限确认可能先于
  「开始调用这个工具」到达客户端。
- 标准输出只能走协议消息：启动后把 sys.stdout 换成标准错误，任何零星的 print 都不会把协议搅乱。
- 项目里没被信任的钩子与 MCP 服务在这里不加载（没法弹信任确认）：在交互模式里打开一次项目、确认信任即可。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import acp
from acp import schema

from heng_agent import __version__, configfile
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.errors import AgentError
from heng_agent.core.types import Notice, TextDelta, ThinkingDelta, ToolFinished, ToolStarted, TurnDone
from heng_agent.hooks import is_trusted, load_hooks
from heng_agent.mcp import MCPManager, load_servers
from heng_agent.mcp import is_trusted as mcp_is_trusted
from heng_agent.model.client import make_client
from heng_agent.model.providers import ENV_PROFILE, load_profiles
from heng_agent.permissions import Permissions, Rules
from heng_agent.session import SessionStore
from heng_agent.tools.base import builtin_tools

#: 权限模式 ↔ ACP 会话模式（客户端里的下拉框）
MODES = {
    "ask": ("每次问", "改文件、跑命令前都问你"),
    "accept-edits": ("改文件不问", "改文件直接改，跑命令前问"),
    "plan": ("只读", "只看不改：读代码、出方案"),
    "auto": ("全部放行", "什么都不问，只在可信的环境里用"),
}
#: 工具 → ACP 的工具类型（客户端据此显示图标）与显示名
TOOL_KIND = {
    "read_file": ("read", "读取"), "write_file": ("edit", "写入"), "edit_file": ("edit", "修改"),
    "bash": ("execute", "执行"), "bash_output": ("execute", "查看输出"), "kill_bash": ("execute", "结束命令"),
    "glob": ("search", "查找文件"), "grep": ("search", "搜索"), "web_fetch": ("fetch", "抓取网页"),
    "web_search": ("fetch", "搜索网页"), "todo": ("think", "任务清单"), "task": ("think", "子任务"),
    "skill": ("read", "读取技能"), "tool_search": ("search", "查找工具"),
}  # fmt: skip
TOOL_RESULT_CHARS = 4000  # 工具结果回给客户端显示的部分（完整的照样给模型）
PLAN_STATUS = {"pending": "pending", "in_progress": "in_progress", "done": "completed"}


@dataclass
class Session:
    agent: Agent
    manager: MCPManager
    provider: str = ""  # 当前用的服务商配置档（模型下拉框的当前值）
    notes: list[str] = field(default_factory=list)  # 第一次提问时告诉用户的事（如跳过了没信任的钩子）


def build_session(cwd: Path, session_id: str | None = None) -> Session:
    """按交互模式的同样方式组装一个智能体：配置 → 服务商 → 钩子 → MCP → 权限规则 → 会话记录。"""
    home = Settings().home
    settings = Settings(home=home, workdir=cwd, **configfile.settings_overrides(home, cwd))
    profiles, default = load_profiles(home, cwd)
    client = make_client(profiles[default], settings.max_tokens)
    notes: list[str] = []
    user_hooks, project_hooks = load_hooks(home, cwd, claude_user=settings.claude_user_config,
                                           skip=settings.claude_hooks_skip)  # fmt: skip
    hooks_ok = is_trusted(home, cwd, project_hooks)
    user_mcp, project_mcp = load_servers(home, cwd)
    mcp_ok = mcp_is_trusted(home, cwd, project_mcp)
    if (project_hooks and not hooks_ok) or (project_mcp and not mcp_ok):
        notes.append("这个项目的钩子 / MCP 服务还没被信任，本次跳过（在终端里运行一次 heng-agent 确认信任即可）")
    manager = MCPManager(user_mcp + (project_mcp if mcp_ok else []), cwd, proxy=settings.proxy)
    notes += [f"MCP：{error}（跳过这个服务）" for error in manager.errors]
    allow, deny, _, _ = configfile.permission_rules(home, cwd, claude_user=settings.claude_user_config)
    store = SessionStore.find(home, cwd, session_id) if session_id else SessionStore(home, cwd)
    agent = Agent(settings, client, tools=[*builtin_tools(), *manager.tools],
                  permissions=Permissions(settings.permission_mode, Rules(allow, deny)), session=store,
                  hooks=user_hooks + (project_hooks if hooks_ok else []))  # fmt: skip
    if session_id:
        agent.resume(store)
    return Session(agent, manager, provider=default, notes=notes)


def modes_state(current: str) -> schema.SessionModeState:
    return schema.SessionModeState(
        current_mode_id=current,
        available_modes=[schema.SessionMode(id=k, name=name, description=desc) for k, (name, desc) in MODES.items()],
    )


def usable_profiles(agent: Agent) -> dict[str, Any]:
    """模型下拉框里列哪些服务商：有密钥（直接写的或环境变量里有）的，加上本机的 heng 与环境变量给的——
    没配密钥的预设列出来也选不了，只会让人困惑。"""
    profiles, _ = load_profiles(agent.settings.home, agent.settings.workdir)

    def usable(name: str, p: Any) -> bool:
        return bool(p.api_key or (p.api_key_env and os.environ.get(p.api_key_env)) or name in ("heng", ENV_PROFILE))

    return {name: p for name, p in profiles.items() if usable(name, p)}


def config_options(session: Session) -> list[Any]:
    """会话配置项（ACP 的标准做法，取代旧的会话模式）：模型、权限模式、思考开关，客户端显示成下拉框。"""
    agent = session.agent
    profiles = usable_profiles(agent)
    if session.provider not in profiles:  # 当前用的总要在列表里
        every, _ = load_profiles(agent.settings.home, agent.settings.workdir)
        profiles[session.provider] = every[session.provider]
    option = schema.SessionConfigSelectOption
    return [
        schema.SessionConfigOptionSelect(
            id="model", name="模型", category="model", type="select", current_value=session.provider,
            options=[option(value=name, name=f"{name} · {p.model}", description=p.note or None)
                     for name, p in profiles.items()]),
        schema.SessionConfigOptionSelect(
            id="mode", name="权限模式", category="mode", type="select", current_value=agent.permissions.mode,
            options=[option(value=k, name=name, description=desc) for k, (name, desc) in MODES.items()]),
        schema.SessionConfigOptionSelect(
            id="thinking", name="思考", category="thought_level", type="select",
            current_value="on" if agent.thinking else "off",
            options=[option(value="off", name="关闭"),
                     option(value="on", name="开启", description="先思考再回答，更慢但更周全")]),
    ]  # fmt: skip


def tool_title(agent: Agent, name: str, args: dict[str, Any]) -> str:
    tool = agent.tools.get(name)
    label = TOOL_KIND.get(name, ("other", name))[1]
    detail = tool.summary(args) if tool is not None else ""
    return f"{label}：{detail}" if detail else label


def tool_preview(name: str, args: dict[str, Any]) -> list[Any] | None:
    """改文件的工具先给出改动对比：客户端在确认框里就能看到要改什么。"""
    path = str(args.get("path", ""))
    if name == "edit_file" and path:
        return [acp.tool_diff_content(path, str(args.get("new_string", "")), str(args.get("old_string", "")))]
    if name == "write_file" and path:
        return [acp.tool_diff_content(path, str(args.get("content", "")), None)]
    return None


def prompt_to_heng(blocks: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    """ACP 的消息块 → (文字, 图片块)：文字直接拼；附带的文件内容加上来源；图片转成 Anthropic 的格式。"""
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for block in blocks:
        kind = getattr(block, "type", "")
        if kind == "text":
            texts.append(block.text)
        elif kind == "image":
            images.append({"type": "image", "source": {"type": "base64", "media_type": block.mime_type,
                                                       "data": block.data}})  # fmt: skip
        elif kind == "resource":
            resource = block.resource
            text = getattr(resource, "text", None)
            texts.append(f"[附带的内容：{resource.uri}]\n{text}" if text is not None else f"[附带：{resource.uri}]")
        elif kind == "resource_link":
            texts.append(f"[附带：{block.uri}]")
    return "\n\n".join(texts), images


class HengAgent(acp.Agent):
    """ACP 里的 heng-agent：每个会话一个 Agent，核心循环在线程池里跑。"""

    def __init__(self) -> None:
        self.conn: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sessions: dict[str, Session] = {}

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, client_capabilities: Any = None, client_info: Any = None,
                         **kwargs: Any) -> schema.InitializeResponse:  # fmt: skip
        self.loop = asyncio.get_running_loop()
        return schema.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=schema.AgentCapabilities(
                load_session=True,
                prompt_capabilities=schema.PromptCapabilities(image=True, embedded_context=True),
                session_capabilities=schema.SessionCapabilities(list=schema.SessionListCapabilities()),
            ),
            agent_info=schema.Implementation(name="heng-agent", title="heng-agent", version=__version__),
            auth_methods=[],
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        return None  # 服务商与密钥在 heng-agent 自己的配置里，不走协议认证

    async def new_session(self, cwd: str, mcp_servers: Any = None, **kwargs: Any) -> schema.NewSessionResponse:
        session = await asyncio.to_thread(build_session, Path(cwd))
        assert session.agent.session is not None
        sid = session.agent.session.id
        self.sessions[sid] = session
        return schema.NewSessionResponse(session_id=sid, modes=modes_state(session.agent.permissions.mode),
                                         config_options=config_options(session))  # fmt: skip

    async def load_session(self, cwd: str, session_id: str, mcp_servers: Any = None,
                           **kwargs: Any) -> schema.LoadSessionResponse:  # fmt: skip
        try:
            session = await asyncio.to_thread(build_session, Path(cwd), session_id)
        except AgentError as exc:
            raise acp.RequestError.invalid_params({"message": str(exc)}) from exc
        self.sessions[session_id] = session
        for message in session.agent.messages:  # 把历史重放给客户端，界面上看得到之前聊了什么
            for block in message["content"] if isinstance(message["content"], list) else []:
                if block.get("type") == "text" and block.get("text"):
                    update = (acp.update_user_message_text(block["text"]) if message["role"] == "user"
                              else acp.update_agent_message_text(block["text"]))  # fmt: skip
                    await self.conn.session_update(session_id=session_id, update=update)
        return schema.LoadSessionResponse(modes=modes_state(session.agent.permissions.mode),
                                          config_options=config_options(session))  # fmt: skip

    async def list_sessions(self, cwd: str | None = None, cursor: str | None = None,
                            **kwargs: Any) -> schema.ListSessionsResponse:  # fmt: skip
        if not cwd:
            return schema.ListSessionsResponse(sessions=[])
        import datetime as dt

        home = Settings().home
        infos = SessionStore.all(home, Path(cwd))
        return schema.ListSessionsResponse(sessions=[
            schema.SessionInfo(session_id=s.id, cwd=cwd, title=s.title or s.first_question[:60] or None,
                               updated_at=dt.datetime.fromtimestamp(s.updated, dt.UTC).isoformat())
            for s in infos
        ])  # fmt: skip

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> None:
        if mode_id not in MODES:
            raise acp.RequestError.invalid_params({"message": f"没有这个模式：{mode_id}"})
        session = self._session(session_id)
        session.agent.permissions.mode = mode_id  # type: ignore[assignment]
        update = schema.ConfigOptionUpdate(
            session_update="config_option_update", config_options=config_options(session)
        )
        await self.conn.session_update(session_id=session_id, update=update)  # 模式下拉框与配置项保持一致

    async def set_config_option(self, config_id: str, session_id: str, value: str | bool,
                                **kwargs: Any) -> schema.SetSessionConfigOptionResponse:  # fmt: skip
        session = self._session(session_id)
        agent = session.agent
        if config_id == "mode" and value in MODES:
            agent.permissions.mode = value  # type: ignore[assignment]
        elif config_id == "thinking" and value in ("on", "off"):
            agent.thinking = value == "on"
        elif config_id == "model":
            profiles, _ = load_profiles(agent.settings.home, agent.settings.workdir)
            if value not in profiles:
                raise acp.RequestError.invalid_params({"message": f"没有这个服务商：{value}"})
            agent.client = make_client(profiles[str(value)], agent.settings.max_tokens)
            session.provider = str(value)
        else:
            raise acp.RequestError.invalid_params({"message": f"不认识的配置：{config_id} = {value}"})
        return schema.SetSessionConfigOptionResponse(config_options=config_options(session))

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            session.agent.cancel()

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> schema.PromptResponse:
        session = self._session(session_id)
        agent = session.agent
        assert self.loop is not None
        loop = self.loop
        outbox: asyncio.Queue[Any] = asyncio.Queue()
        text, images = prompt_to_heng(prompt)
        for note in session.notes:
            outbox.put_nowait(acp.update_agent_thought_text(f"{note}\n"))
        session.notes.clear()
        current_call: dict[str, str] = {}  # 最近开始的工具调用：权限确认要带上它的 id
        said: dict[str, bool] = {"text": False, "tool_since": False}  # 用来在「工具调用之后的新一段话」前加空行

        def send(update: Any) -> None:  # 在干活的线程里调用：按顺序排进队列
            loop.call_soon_threadsafe(outbox.put_nowait, update)

        def on_event(event: Any) -> None:
            if isinstance(event, TextDelta):
                # 客户端把文字片段拼成一条消息：调完工具后的新一段话不加空行会和前一段连在一起
                # （实测：「……first.包邮门槛……」）
                prefix = "\n\n" if said["text"] and said["tool_since"] else ""
                said["text"], said["tool_since"] = True, False
                send(acp.update_agent_message_text(prefix + event.text))
            elif isinstance(event, ThinkingDelta):
                send(acp.update_agent_thought_text(event.text))
            elif isinstance(event, Notice):
                send(acp.update_agent_thought_text(f"\n⋯ {event.text}\n"))
            elif isinstance(event, ToolStarted):
                current_call["id"] = event.id
                said["tool_since"] = True
                kind = TOOL_KIND.get(event.name, ("other", event.name))[0]
                send(acp.start_tool_call(event.id, tool_title(agent, event.name, event.input), kind=kind,  # type: ignore[arg-type]
                                         status="in_progress", raw_input=event.input,
                                         content=tool_preview(event.name, event.input)))  # fmt: skip
            elif isinstance(event, ToolFinished):
                status = "failed" if event.denied or event.result.is_error else "completed"
                shown = "（用户拒绝了这次操作）" if event.denied else event.result.content[:TOOL_RESULT_CHARS]
                send(acp.update_tool_call(event.id, status=status,  # type: ignore[arg-type]
                                          content=[acp.tool_content(acp.text_block(shown))]))  # fmt: skip
                if event.name == "todo" and agent.ctx.todos:
                    send(acp.update_plan([acp.plan_entry(t["content"], status=PLAN_STATUS.get(t["status"], "pending"))  # type: ignore[arg-type]
                                          for t in agent.ctx.todos]))  # fmt: skip

        def approve(tool: Any, args: dict[str, Any]) -> str:
            """权限确认：排进同一个队列（保证在「开始调用」之后到达），等客户端回复。"""
            answer: concurrent.futures.Future[str] = concurrent.futures.Future()
            # 带上工具类型与改动对比：客户端弹确认框时多半只看这条请求，不带就看不到要改什么
            request = schema.ToolCallUpdate(tool_call_id=current_call.get("id", "tool"),
                                            title=tool_title(agent, tool.name, args), raw_input=args,
                                            kind=TOOL_KIND.get(tool.name, ("other", ""))[0],  # type: ignore[arg-type]
                                            content=tool_preview(tool.name, args))  # fmt: skip
            send(("permission", request, answer))
            try:
                return answer.result()
            except Exception:  # 连接断了等：当作拒绝，绝不在没问到的情况下放行
                return "deny"

        async def pump() -> None:  # 按顺序把队列里的东西发给客户端
            while True:
                item = await outbox.get()
                if item is None:
                    return
                if isinstance(item, tuple) and item[0] == "permission":
                    _, request, answer = item
                    answer.set_result(await self._ask_permission(session_id, request))
                    continue
                await self.conn.session_update(session_id=session_id, update=item)

        pumping = asyncio.create_task(pump())
        stop: str = "end_turn"
        try:
            done = await asyncio.to_thread(agent.run, text, on_event, approve, images or None)
            if isinstance(done, TurnDone) and done.rounds >= agent.settings.max_rounds:
                stop = "max_turn_requests"
        except KeyboardInterrupt:
            stop = "cancelled"
        except AgentError as exc:
            send(acp.update_agent_message_text(f"\n\n⚠ 出错了：{exc}"))
        finally:
            loop.call_soon_threadsafe(outbox.put_nowait, None)
            await pumping
        return schema.PromptResponse(stop_reason=stop)  # type: ignore[arg-type]

    async def _ask_permission(self, session_id: str, request: Any) -> str:
        options = [
            schema.PermissionOption(option_id="once", name="允许这一次", kind="allow_once"),
            schema.PermissionOption(option_id="always", name="本会话都允许", kind="allow_always"),
            schema.PermissionOption(option_id="deny", name="拒绝", kind="reject_once"),
        ]
        response = await self.conn.request_permission(session_id=session_id, tool_call=request, options=options)
        outcome = response.outcome
        return outcome.option_id if getattr(outcome, "outcome", "") == "selected" else "deny"  # 取消 = 拒绝

    def _session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise acp.RequestError.invalid_params({"message": f"没有这个会话：{session_id}"})
        return session

    def close(self) -> None:
        for session in self.sessions.values():
            session.manager.close()


class _ProtocolStdout:
    """--acp 时 sys.stdout 的替身：字节流（buffer、fileno）仍是真正的标准输出，SDK 用它收发协议消息；
    文字写入（零星的 print）改去标准错误，不会把协议搅乱。

    不能直接把 sys.stdout 换成标准错误：Windows 上 SDK 每次发消息都现取 sys.stdout.buffer，
    那样协议消息也跟着去了标准错误（看过 SDK 源码）；Linux / macOS 上它用 fileno 接管道，也得是真的。
    """

    def __init__(self, real: Any, stray: Any) -> None:
        self.buffer = real.buffer
        self._real = real
        self._stray = stray

    def fileno(self) -> int:
        return int(self._real.fileno())

    def write(self, text: str) -> int:
        return int(self._stray.write(text))

    def flush(self) -> None:
        self._stray.flush()
        with contextlib.suppress(Exception):
            self._real.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stray, name)


def run() -> int:
    """heng-agent --acp 的入口。"""
    sys.stdout = _ProtocolStdout(sys.stdout, sys.stderr)  # type: ignore[assignment]
    agent = HengAgent()
    try:
        asyncio.run(acp.run_agent(agent))
    except KeyboardInterrupt:
        pass
    finally:
        agent.close()
    return 0

"""heng-agent --acp 的端到端测试：用官方 SDK 的客户端真的启动子进程，走真实的标准输入输出（假模型，不花钱）。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import acp
import pytest
from acp import schema

AGENT = str(Path(__file__).resolve().parent / "acp_fake_agent.py")


class RecordingClient(acp.Client):
    """记下收到的一切（按到达顺序）；权限确认按 answer 回答。"""

    def __init__(self, answer: str = "once") -> None:
        self.answer = answer
        self.events: list[tuple[str, Any]] = []
        self.tool_started = asyncio.Event()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.events.append(("update", update))
        if getattr(update, "session_update", "") == "tool_call":
            self.tool_started.set()

    async def request_permission(self, session_id: str, tool_call: Any, options: Any,
                                 **kwargs: Any) -> schema.RequestPermissionResponse:  # fmt: skip
        self.events.append(("permission", tool_call))
        if self.answer == "cancel":
            return schema.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(option_id=self.answer, outcome="selected")
        )

    def kinds(self) -> list[str]:
        return [e[0] if e[0] == "permission" else e[1].session_update for e in self.events]

    def text(self) -> str:
        return "".join(e[1].content.text for e in self.events
                       if e[0] == "update" and e[1].session_update == "agent_message_chunk")  # fmt: skip


def _env(tmp_path: Path, script: list[Any]) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"HENG_AGENT_HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
                "HENG_FAKE_SCRIPT": json.dumps(script), "PYTHONIOENCODING": "utf-8"})  # fmt: skip
    return env


def _run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, 60))


def test_prompt_with_permission(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    marker = work / "ran.txt"
    command = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('ok')\""
    script = [["我先跑一下", ["tool", "bash", {"command": command}]], ["命令跑完了"]]
    client = RecordingClient("once")

    async def scenario() -> tuple[Any, Any, Any]:
        async with acp.spawn_agent_process(client, sys.executable, AGENT, env=_env(tmp_path, script)) as (conn, _):
            init = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(work), mcp_servers=[])
            result = await conn.prompt(session_id=session.session_id, prompt=[acp.text_block("跑一下")])
            return init, session, result

    init, session, result = _run(scenario())
    assert init.agent_info.name == "heng-agent" and init.agent_capabilities.load_session
    assert session.modes.current_mode_id == "ask" and {m.id for m in session.modes.available_modes} >= {"plan", "auto"}
    assert result.stop_reason == "end_turn"
    kinds = client.kinds()
    assert kinds.index("tool_call") < kinds.index("permission")  # 先告诉客户端要调什么工具，再请它确认
    assert marker.read_text(encoding="utf-8") == "ok"  # 允许之后命令真的跑了
    finished = [e[1] for e in client.events if e[0] == "update" and e[1].session_update == "tool_call_update"]
    assert finished and finished[-1].status == "completed"
    assert "命令跑完了" in client.text()  # 零星的 print 没把协议搅乱，后面的消息照样收到
    assert "我先跑一下\n\n命令跑完了" in client.text()  # 调完工具后的新一段话另起一段，不和前一段连在一起


def test_denied_tool_does_not_run(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    marker = work / "ran.txt"
    command = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('ok')\""
    client = RecordingClient("cancel")  # 用户关掉了确认框

    async def scenario() -> Any:
        env = _env(tmp_path, [[["tool", "bash", {"command": command}]], ["好吧"]])
        async with acp.spawn_agent_process(client, sys.executable, AGENT, env=env) as (conn, _):
            await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(work), mcp_servers=[])
            return await conn.prompt(session_id=session.session_id, prompt=[acp.text_block("跑一下")])

    assert _run(scenario()).stop_reason == "end_turn"
    assert not marker.exists()  # 没确认就不执行
    finished = [e[1] for e in client.events if e[0] == "update" and e[1].session_update == "tool_call_update"]
    assert finished[-1].status == "failed"


def test_cancel_long_command(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    sleeper = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    client = RecordingClient()

    async def scenario() -> tuple[Any, float]:
        env = _env(tmp_path, [[["tool", "bash", {"command": sleeper}]]])
        async with acp.spawn_agent_process(client, sys.executable, AGENT, env=env) as (conn, _):
            await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(work), mcp_servers=[])
            await conn.set_session_mode(session_id=session.session_id, mode_id="auto")  # 不问，直接跑
            running = asyncio.create_task(conn.prompt(session_id=session.session_id, prompt=[acp.text_block("等等")]))
            await client.tool_started.wait()
            await asyncio.sleep(1)
            begin = time.monotonic()
            await conn.cancel(session_id=session.session_id)
            result = await running
            return result, time.monotonic() - begin

    result, took = _run(scenario())
    assert result.stop_reason == "cancelled" and took < 10  # 很快停下，不是等 30 秒的命令自己结束


def test_list_and_load_session(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()

    async def first() -> str:
        async with acp.spawn_agent_process(RecordingClient(), sys.executable, AGENT,
                                           env=_env(tmp_path, [["第一次的回答"]])) as (conn, _):  # fmt: skip
            await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(work), mcp_servers=[])
            await conn.prompt(session_id=session.session_id, prompt=[acp.text_block("第一个问题")])
            return session.session_id

    sid = _run(first())
    client = RecordingClient()

    async def second() -> Any:
        async with acp.spawn_agent_process(client, sys.executable, AGENT, env=_env(tmp_path, [])) as (conn, _):
            await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            listed = await conn.list_sessions(cwd=str(work))
            await conn.load_session(cwd=str(work), session_id=sid, mcp_servers=[])
            return listed

    listed = _run(second())
    assert [s.session_id for s in listed.sessions] == [sid] and "第一个问题" in (listed.sessions[0].title or "")
    replayed = [e[1] for e in client.events if e[0] == "update"]
    assert [u.session_update for u in replayed] == ["user_message_chunk", "agent_message_chunk"]  # 历史重放
    assert "第一次的回答" in client.text()


def test_set_mode_rejects_unknown(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()

    async def scenario() -> None:
        async with acp.spawn_agent_process(RecordingClient(), sys.executable, AGENT,
                                           env=_env(tmp_path, [])) as (conn, _):  # fmt: skip
            await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(work), mcp_servers=[])
            await conn.set_session_mode(session_id=session.session_id, mode_id="plan")
            with pytest.raises(acp.RequestError):
                await conn.set_session_mode(session_id=session.session_id, mode_id="乱写")

    _run(scenario())


def test_config_options_model_mode_thinking(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    client = RecordingClient()
    env = _env(tmp_path, [])
    env.update({"HENG_AGENT_BASE_URL": "https://cc.example.cn", "HENG_AGENT_API_KEY": "sk-x",
                "HENG_AGENT_MODEL": "claude-sonnet-5"})  # fmt: skip

    async def scenario() -> tuple[Any, Any]:
        async with acp.spawn_agent_process(client, sys.executable, AGENT, env=env) as (conn, _):
            await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            session = await conn.new_session(cwd=str(work), mcp_servers=[])
            sid = session.session_id
            await conn.set_config_option(config_id="thinking", session_id=sid, value="on")
            changed = await conn.set_config_option(config_id="model", session_id=sid, value="heng")
            await conn.set_session_mode(session_id=sid, mode_id="plan")  # 旧接口：也要通知客户端刷新配置项
            with pytest.raises(acp.RequestError):
                await conn.set_config_option(config_id="model", session_id=sid, value="没有这个")
            return session, changed

    session, changed = _run(scenario())
    options = {o.id: o for o in session.config_options}
    assert [o.id for o in session.config_options] == ["model", "mode", "thinking"]
    assert options["model"].category == "model" and options["model"].current_value == "env"  # 环境变量给的是默认
    assert "env · claude-sonnet-5" in [o.name for o in options["model"].options]
    after = {o.id: o.current_value for o in changed.config_options}
    assert after == {"model": "heng", "mode": "ask", "thinking": "on"}
    updates = [e[1] for e in client.events if e[0] == "update" and e[1].session_update == "config_option_update"]
    assert updates and {o.id: o.current_value for o in updates[-1].config_options}["mode"] == "plan"

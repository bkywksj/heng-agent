"""第三阶段：子智能体（只读、并行、用量汇总、不能再派子智能体）。"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.types import Message, ModelEvent, TextDelta, ToolCall, ToolFinished, TurnEnd


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    work = tmp_path / "proj"
    work.mkdir()
    (work / "a.py").write_text("def alpha(): pass\n", encoding="utf-8")
    (work / "b.py").write_text("def beta(): pass\n", encoding="utf-8")
    return Settings(workdir=work, home=tmp_path / "home")


class RoutingModel:
    """按对话内容决定回复的假模型（并发调用时也确定）：

    - 子智能体（第一条消息是调研任务）：第一轮 grep，第二轮报告结论；每次回复前等 delay 秒，用来证明是并行的；
    - 主智能体：第一轮同时派两个 task，拿到结果后总结。
    """

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.tool_sets: list[list[str]] = []

    def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> Iterator[ModelEvent]:
        with self.lock:
            self.tool_sets.append([t["name"] for t in tools])
        first = messages[0]["content"][0]["text"]
        if "调研子智能体" in first:
            time.sleep(self.delay)
            target = "alpha" if "alpha" in first else "beta"
            if len(messages) == 1:
                yield ToolCall(id=f"g-{target}", name="grep", input={"pattern": f"def {target}"})
                yield TurnEnd("tool_use", {"input_tokens": 100, "output_tokens": 10})
            else:
                found = messages[-1]["content"][0]["content"]
                yield TextDelta(f"{target} 定义在 {found.split(':')[0]}")
                yield TurnEnd("end_turn", {"input_tokens": 100, "output_tokens": 10})
            return
        if len(messages) == 1:
            yield ToolCall(id="t1", name="task", input={"description": "找 alpha", "prompt": "找 alpha 定义在哪"})
            yield ToolCall(id="t2", name="task", input={"description": "找 beta", "prompt": "找 beta 定义在哪"})
            yield TurnEnd("tool_use", {"input_tokens": 1, "output_tokens": 1})
        else:
            yield TextDelta("两个都找到了")
            yield TurnEnd("end_turn", {"input_tokens": 1, "output_tokens": 1})


def test_subagents_run_in_parallel_and_report_back(settings: Settings) -> None:
    model = RoutingModel(delay=0.5)
    agent = Agent(settings, model)
    events: list[Any] = []
    started = time.monotonic()
    agent.run("alpha 和 beta 分别定义在哪？", events.append, lambda t, a: "deny")
    elapsed = time.monotonic() - started
    # 每个子智能体调两次模型、每次等 0.5 秒：串行至少 2 秒，并行约 1 秒
    assert elapsed < 1.8, f"子智能体没有并行（用了 {elapsed:.1f} 秒）"
    results = [e for e in events if isinstance(e, ToolFinished) and e.name == "task"]
    assert [r.id for r in results] == ["t1", "t2"] or {r.id for r in results} == {"t1", "t2"}
    fed = agent.messages[2]["content"]
    assert [b["tool_use_id"] for b in fed] == ["t1", "t2"]  # 结果按原顺序交回模型
    assert "alpha 定义在 a.py" in fed[0]["content"] and "beta 定义在 b.py" in fed[1]["content"]
    # 两个子智能体各两次调用 × (100 + 10)，加上主智能体两次 × (1 + 1)
    assert agent.usage == {"input_tokens": 402, "output_tokens": 42, "cache_read_tokens": 0, "cache_write_tokens": 0}


def test_subagent_is_read_only_and_cannot_spawn_more(settings: Settings) -> None:
    model = RoutingModel()
    Agent(settings, model).run("找", lambda e: None, lambda t, a: "deny")
    main_tools, sub_tools = model.tool_sets[0], model.tool_sets[1]
    assert "task" in main_tools and "bash" in main_tools
    assert set(sub_tools) == {"read_file", "glob", "grep"}  # 没有写文件、跑命令、读网页，也不能再派子智能体


# ---------- 图片 ----------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # 够用的假 PNG：只测编码与转发，不解码


def test_mention_images_and_send_to_model(settings: Settings) -> None:
    import base64

    from fake_model import FakeModel
    from heng_agent.compaction import transcript
    from heng_agent.context import MAX_IMAGE_BYTES, mention_images
    from heng_agent.model.client import to_openai_messages

    (settings.workdir / "shot.png").write_bytes(PNG)
    (settings.workdir / "huge.png").write_bytes(b"\x89PNG" + b"\x00" * (MAX_IMAGE_BYTES + 1))
    images, attached = mention_images("看 @shot.png 和 @huge.png 还有 @a.py", settings.workdir)
    assert attached == ["shot.png"]  # 超过 5 MB 与非图片都不附
    assert images[0]["source"] == {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}

    model = FakeModel([["界面上按钮错位了"]])
    agent = Agent(settings, model)
    agent.run("看看这张截图", lambda e: None, lambda t, a: "deny", images=images)
    sent = model.calls[0][0]["content"]
    assert sent[0] == {"type": "text", "text": "看看这张截图"} and sent[1]["type"] == "image"

    openai = to_openai_messages("", agent.messages[:1])[0]["content"]
    assert openai[0] == {"type": "text", "text": "看看这张截图"}
    assert openai[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "【用户附图】" in transcript(agent.messages, 10_000)  # 压缩时不把 base64 塞进摘要


# ---------- MCP ----------

FAKE_SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


def _server(name: str = "calc", **env: str) -> Any:
    import sys

    from heng_agent.mcp import ServerConfig

    return ServerConfig(name, sys.executable, [FAKE_SERVER], dict(env), timeout=20)


def test_mcp_handshake_list_call_and_close(settings: Settings) -> None:
    from heng_agent.mcp import MCPManager

    manager = MCPManager([_server()], settings.workdir)
    client = manager.clients["calc"]  # close() 会清空 clients：先拿住，关完再看进程
    try:
        assert not manager.errors
        tools = {t.name: t for t in manager.tools}
        assert set(tools) == {"mcp__calc__add", "mcp__calc__fail"}  # 两页都拿到了；日志噪音被跳过
        ctx = None
        assert tools["mcp__calc__add"].run({"a": 2, "b": 40}, ctx).content == "42"  # type: ignore[arg-type]
        failed = tools["mcp__calc__fail"].run({}, ctx)  # type: ignore[arg-type]
        assert failed.is_error and failed.content == "出错了"
        assert tools["mcp__calc__add"].kind == "mcp" and "[MCP calc]" in tools["mcp__calc__add"].description
    finally:
        manager.close()
    assert client.proc.poll() is not None  # type: ignore[attr-defined]  # 进程已关掉
    manager.close()  # 再关一次也不出错（界面与命令行入口退出时都会关）


def test_one_bad_server_does_not_close_good_ones(settings: Settings) -> None:
    from heng_agent.mcp import MCPManager, ServerConfig

    bad = ServerConfig("bad", "this-command-does-not-exist-xyz", [], {}, timeout=5)
    manager = MCPManager([_server(), bad], settings.workdir)
    try:
        assert len(manager.errors) == 1 and "bad" in manager.errors[0]
        assert next(iter(manager.clients.values())).proc.poll() is None  # 前面启动成功的服务没有被误关
        assert manager.tools[0].run({"a": 1, "b": 1}, None).content == "2"  # type: ignore[arg-type]
    finally:
        manager.close()


def test_agent_calls_mcp_tool_with_permission(settings: Settings) -> None:
    from fake_model import FakeModel
    from heng_agent.mcp import MCPManager
    from heng_agent.tools.base import builtin_tools

    manager = MCPManager([_server()], settings.workdir)
    try:
        model = FakeModel([[("tool", "mcp__calc__add", {"a": 3, "b": 4})], ["是 7"]])
        asked: list[str] = []
        agent = Agent(settings, model, tools=[*builtin_tools(), *manager.tools])
        agent.run("3 加 4", lambda e: None, lambda t, a: asked.append(t.name) or "once")
        assert asked == ["mcp__calc__add"]  # 外部工具要确认
        assert model.calls[-1][-1]["content"][0]["content"] == "7"
    finally:
        manager.close()


def test_project_mcp_needs_trust_in_print_mode(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import sys

    from fake_model import FakeModel
    from heng_agent import cli
    from heng_agent.mcp import is_trusted, load_servers, trust

    marker = settings.workdir / "mcp-started.txt"
    cfg = {
        "mcp_servers": {
            "calc": {"command": sys.executable, "args": [FAKE_SERVER], "env": {"FAKE_MCP_MARKER": str(marker)}}
        }
    }
    (settings.workdir / ".heng-agent").mkdir()
    (settings.workdir / ".heng-agent/config.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("HENG_AGENT_HOME", str(settings.home))
    monkeypatch.setattr(cli, "make_client", lambda profile, max_tokens: FakeModel([["好"]]))
    cli.main(["-p", "hi", "--workdir", str(settings.workdir)])
    assert not marker.exists()  # 没信任：项目 MCP 服务不启动
    _, project = load_servers(settings.home, settings.workdir)
    trust(settings.home, settings.workdir, project)
    cli.main(["-p", "hi", "--workdir", str(settings.workdir)])
    assert marker.read_text(encoding="utf-8") == "started\n"  # 信任后启动，且退出时已关掉（只启动一次）
    cfg["mcp_servers"]["calc"]["args"].append("--changed")
    (settings.workdir / ".heng-agent/config.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert not is_trusted(settings.home, settings.workdir, load_servers(settings.home, settings.workdir)[1])

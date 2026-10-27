"""第一档剩余：提示缓存、权限持久规则、/permissions、/model 选择器、/resume 与 /rename、-c。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent import cli, configfile
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.model.client import make_client, parse_sse, with_cache_marks
from heng_agent.model.providers import Profile, load_profiles
from heng_agent.permissions import Permissions, Rules
from heng_agent.session import SessionStore
from heng_agent.tools.base import builtin_tools
from heng_agent.ui import repl as repl_module
from heng_agent.ui.repl import Repl

TOOLS = {t.name: t for t in builtin_tools()}


# ---------- 提示缓存 ----------


def test_cache_marks_do_not_touch_history() -> None:
    history: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "第一问"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "答"}]},
        {"role": "user", "content": [{"type": "text", "text": "第二问"}, {"type": "image", "source": {}}]},
    ]
    before = copy.deepcopy(history)
    system, sent = with_cache_marks("系统提示词", history)
    assert system == [{"type": "text", "text": "系统提示词", "cache_control": {"type": "ephemeral"}}]
    assert sent[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}  # 断点在最后一条消息的最后一块
    assert sum("cache_control" in b for m in sent for b in m["content"]) == 1  # 只一个：标记不能越积越多
    assert history == before  # 对话历史本身没被改


def test_cache_usage_parsed_and_counted(tmp_path: Path) -> None:
    def sse(events: list[dict[str, Any]]) -> list[str]:
        return [f"data: {json.dumps(e)}" for e in events]

    lines = sse([
        {"type": "message_start", "message": {"usage": {"input_tokens": 12, "cache_read_input_tokens": 4000,
                                                         "cache_creation_input_tokens": 300}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ])  # fmt: skip
    end = list(parse_sse(iter(lines)))[-1]
    assert end.usage == {"input_tokens": 12, "cache_read_tokens": 4000, "cache_write_tokens": 300, "output_tokens": 3}

    class OneShot:
        def stream(self, *a: Any) -> Any:
            yield from parse_sse(iter(lines))

    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), OneShot())
    agent.run("hi", lambda e: None, lambda t, a: "deny")
    assert agent.last_input_tokens == 4312  # 上下文占用 = 没命中 + 命中 + 写入：只看 input_tokens 会低估成 12
    profile = Profile(protocol="anthropic", base_url="http://x", model="m", price_in=10, price_out=50)
    money = profile.cost(agent.usage)
    assert money == pytest.approx((12 + 4000 * 0.1 + 300 * 1.25) * 10 / 1e6 + 3 * 50 / 1e6)


def test_prompt_cache_can_be_turned_off() -> None:
    import httpx

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"type": "message_stop"}\n\n')

    for cache in (True, False):
        profile = Profile(protocol="anthropic", base_url="http://x", model="m", prompt_cache=cache)
        client = make_client(profile, 10, transport=httpx.MockTransport(handler))
        list(client.stream("系统", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}], []))
    assert isinstance(bodies[0]["system"], list) and "cache_control" in bodies[0]["messages"][-1]["content"][-1]
    assert bodies[1]["system"] == "系统" and "cache_control" not in bodies[1]["messages"][-1]["content"][-1]


# ---------- 权限规则 ----------


def test_rules_matching_and_precedence() -> None:
    bash, write, mcp_like = TOOLS["bash"], TOOLS["write_file"], TOOLS["read_file"]
    rules = Rules(allow=["bash(git status*)", "bash(npm test)", "write_file(src/*)"], deny=["bash(git push*)"])
    perms = Permissions("ask", rules)
    assert perms.decide(bash, {"command": "git status -s"}) == "allow"
    assert perms.decide(bash, {"command": "npm test"}) == "allow"
    assert perms.decide(bash, {"command": "npm install"}) == "ask"
    assert perms.decide(write, {"path": "src/a.py"}) == "allow" and perms.decide(write, {"path": "a.py"}) == "ask"
    assert perms.decide(bash, {"command": "git push origin"}) == "deny"
    assert Permissions("auto", rules).decide(bash, {"command": "git push -f"}) == "deny"  # 拒绝在 auto 下也生效
    assert Permissions("plan", rules).decide(bash, {"command": "npm test"}) == "deny"  # plan 的只读约束盖过允许
    assert perms.decide(mcp_like, {"path": "x"}) == "allow"  # 读文件本来就不用问


@pytest.mark.parametrize("command", [
    "git status && rm -rf src", "git status; del x", "git status | sh", "git status > out.txt",
    "git status `whoami`", "git status $(whoami)", "git status\nrm -rf src", "git status || shutdown",
])  # fmt: skip
def test_compound_commands_never_match_allow_rules(command: str) -> None:
    perms = Permissions("ask", Rules(allow=["bash(git status*)", "bash"]))  # 连「bash」整个放开也不行
    assert perms.decide(TOOLS["bash"], {"command": command}) == "ask"


def test_persist_answer_adds_exact_command_rule() -> None:
    perms = Permissions("ask")
    saved: list[str] = []
    perms.on_persist = saved.append
    assert perms.check(TOOLS["bash"], {"command": "pytest -q"}, lambda t, a: "persist")
    assert saved == ["bash(pytest -q)"]  # 只放开这一条命令，不是整个 bash
    assert perms.decide(TOOLS["bash"], {"command": "pytest -q"}) == "allow"
    assert perms.decide(TOOLS["bash"], {"command": "rm x"}) == "ask"


def test_project_config_cannot_allow(tmp_path: Path) -> None:
    home, work = tmp_path / "h", tmp_path / "w"
    work.mkdir()
    configfile.save(home / "config.json", {"permissions": {"allow": ["bash(npm test)"], "deny": ["bash(git push*)"]}})
    configfile.save(
        work / ".heng-agent/config.json", {"permissions": {"allow": ["bash(*)"], "deny": ["write_file(.env*)"]}}
    )
    allow, deny, ignored, _ = configfile.permission_rules(home, work)
    assert allow == ["bash(npm test)"] and ignored == ["bash(*)"]  # 项目里的允许规则不生效
    assert deny == ["bash(git push*)", "write_file(.env*)"]  # 项目可以加拒绝


# ---------- 界面里的命令 ----------


def _repl(tmp_path: Path, model: Any = None) -> tuple[Repl, Console]:
    home, work = tmp_path / "home", tmp_path / "proj"
    work.mkdir(exist_ok=True)
    configfile.save(home / "config.json", {"default": "yy", "profiles": {"yy": {
        "protocol": "anthropic", "base_url": "https://relay.example", "model": "m1"}}})  # fmt: skip
    profiles, _ = load_profiles(home, work)
    agent = Agent(Settings(workdir=work, home=home), model or make_client(profiles["yy"], 100),
                  session=SessionStore(home, work))  # fmt: skip
    console = Console(record=True, width=160, force_terminal=False)
    return Repl(agent, console=console, provider_name="yy"), console


def test_permissions_command(tmp_path: Path) -> None:
    repl, console = _repl(tmp_path)
    repl.handle_command("/permissions allow bash(npm test)")
    repl.handle_command("/permissions deny bash(git push*) --project")
    repl.handle_command("/permissions allow bash(*) --project")  # 项目配置加允许：拒绝
    rules = repl.agent.permissions.rules
    assert rules.allow == ["bash(npm test)"] and rules.deny == ["bash(git push*)"]  # 立即生效
    assert "只能加拒绝规则" in console.export_text()
    repl.handle_command("/permissions remove bash(npm test)")
    assert repl.agent.permissions.rules.allow == []
    repl.handle_command("/permissions")
    assert "bash(git push*)" in console.export_text()


def test_model_picker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repl, _ = _repl(tmp_path)
    monkeypatch.setattr(repl_module, "list_models", lambda profile: ["m1", "m2", "m3"])
    answers = iter(["2", "y"])  # 选择器用不了时退回打字：选第 2 个，存成默认
    monkeypatch.setattr(repl, "_ask", lambda prompt="": next(answers))
    repl.handle_command("/model")
    assert repl.agent.client.profile.model == "m2"
    saved = configfile.load(repl.agent.settings.home / "config.json")
    assert saved["profiles"]["yy"]["model"] == "m2"


def test_save_model_keeps_preset(tmp_path: Path) -> None:
    from heng_agent.ui.config_cmd import ConfigCommand

    repl, _ = _repl(tmp_path)
    ConfigCommand(repl).save_model("deepseek", "deepseek-chat")
    profiles, _ = load_profiles(repl.agent.settings.home, repl.agent.settings.workdir)
    assert profiles["deepseek"].model == "deepseek-chat" and "deepseek" in profiles["deepseek"].base_url  # 预设地址没丢


def test_resume_picker_and_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repl, _ = _repl(tmp_path, FakeModel([["一"], ["二"]]))
    a = repl.agent
    a.run("旧会话的问题", lambda e: None, lambda t, x: "deny")
    repl.handle_command("/rename 登录模块重构")
    old_id = a.session.id
    repl.handle_command("/clear")
    a.run("新会话的问题", lambda e: None, lambda t, x: "deny")
    assert a.turns  # 新会话有可撤销的提问
    monkeypatch.setattr(repl, "_ask", lambda prompt="": "1")  # 按关键词筛出 1 个，选它
    repl.handle_command("/resume 登录")
    assert a.session.id == old_id and a.session.title == "登录模块重构"
    assert a.turns == [] and a.messages[0]["content"][0]["text"] == "旧会话的问题"  # 撤销点不带过来
    infos = SessionStore.all(a.settings.home, a.settings.workdir)
    assert {i.title for i in infos} == {"登录模块重构", ""}


def test_continue_flag() -> None:
    assert cli.build_parser().parse_args(["-c"]).resume == ""  # -c = 接最近的会话
    assert cli.build_parser().parse_args(["-r", "2026"]).resume == "2026"

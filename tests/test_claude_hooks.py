"""兼容 Claude Code 的钩子：读 settings.json 的 hooks，照它的约定跑（标准输入 JSON、Claude 工具名、JSON 决定）。

测试用的钩子脚本用 Python 写（不依赖 node），输入输出完全照 Claude 的约定：每次调用把收到的 JSON 记进日志。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.hooks import Hook, load_hooks

HOOK_SCRIPT = r"""
import json, os, sys
data = json.loads(sys.stdin.read())
data["_env_project_dir"] = os.environ.get("CLAUDE_PROJECT_DIR")
with open(sys.argv[1], "a", encoding="utf-8") as log:
    log.write(json.dumps(data, ensure_ascii=False) + "\n")
mode, event = sys.argv[2], data["hook_event_name"]
if mode == "guard" and event == "PreToolUse":
    cmd = data["tool_input"].get("command", "")
    if data["tool_name"] == "Bash" and "rm -rf" in cmd:
        print(json.dumps({"decision": "block", "reason": "不许删整个目录"}))  # 老写法：退出码 0 + JSON
    fp = data["tool_input"].get("file_path", "")
    if data["tool_name"] == "Write" and fp.endswith(".bat"):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": "不许写 .bat"}}))  # 新写法
elif mode == "exit2":
    sys.stderr.write("退出码 2 拦下：危险命令")
    sys.exit(2)
elif mode == "post" and event == "PostToolUse":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                             "additionalContext": "格式检查：通过"}}))
elif mode == "prompt" and event == "UserPromptSubmit":
    if "密码" in data["prompt"]:
        print(json.dumps({"decision": "block", "reason": "消息里有密码，拦下"}))
    else:
        print("当前分支是 main")  # 纯文字：当作补充上下文
elif mode == "stop" and event == "Stop":
    if not data["stop_hook_active"] and "English" in data["last_assistant_message"]:
        print(json.dumps({"decision": "block", "reason": "请用中文重新回答"}))
"""


def _setup(tmp_path: Path, hooks: dict[str, Any], *, project: bool = False) -> tuple[Settings, Path]:
    """写一个钩子脚本和 Claude 的 settings.json，返回 (设置, 日志文件)。"""
    work = tmp_path / "proj"
    work.mkdir(exist_ok=True)
    script = tmp_path / "hook.py"
    script.write_text(HOOK_SCRIPT, encoding="utf-8")
    log = tmp_path / "hook.log"
    target = work / ".claude" / "settings.json" if project else Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    def command(mode: str) -> dict[str, Any]:
        return {"type": "command", "command": f'"{sys.executable}" "{script}" "{log}" {mode}', "timeout": 20}

    config = {event: [{**({"matcher": m} if m else {}), "hooks": [command(mode)]} for m, mode in groups]
              for event, groups in hooks.items()}  # fmt: skip
    config["PreCompact"] = [{"hooks": [command("x")]}]  # 不支持的事件：跳过
    target.write_text(json.dumps({"hooks": config}), encoding="utf-8")
    return Settings(workdir=work, home=tmp_path / "heng-home", permission_mode="auto"), log


def _agent(settings: Settings, script: list[Any]) -> tuple[Agent, FakeModel]:
    user, project = load_hooks(settings.home, settings.workdir)
    model = FakeModel(script)
    return Agent(settings, model, hooks=user + project), model


def _log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_load_claude_hooks(tmp_path: Path) -> None:
    settings, _ = _setup(tmp_path, {"PreToolUse": [("Bash|PowerShell", "guard")], "Stop": [("", "stop")]})
    _setup(tmp_path, {"PostToolUse": [("Write|Edit", "post")]}, project=True)
    user, project = load_hooks(settings.home, settings.workdir)
    assert [(h.event, h.match, h.style) for h in user] == [
        ("pre_tool", "Bash|PowerShell", "claude"),
        ("stop", ".*", "claude"),
    ]
    assert [(h.event, h.source) for h in project] == [("post_tool", "project")]  # 项目里的：要确认信任才跑
    assert not any(h.event not in ("pre_tool", "post_tool", "stop") for h in user + project)  # PreCompact 跳过
    assert load_hooks(settings.home, settings.workdir, claude_user=False)[0] == []  # 关掉用户级


def test_pre_tool_json_decisions_block(tmp_path: Path) -> None:
    settings, log = _setup(tmp_path, {"PreToolUse": [("Bash|Write|Edit", "guard")]})
    agent, model = _agent(settings, [
        [("tool", "bash", {"command": "rm -rf build"}), ("tool", "write_file", {"path": "run.bat", "content": "x"}),
         ("tool", "write_file", {"path": "ok.txt", "content": "好"})],
        ["好的"],
    ])  # fmt: skip
    agent.run("清理一下", lambda e: None, lambda t, a: "once")
    results = [b for b in model.calls[1][-1]["content"]]
    assert results[0]["is_error"] and "不许删整个目录" in results[0]["content"]  # 退出码 0 + 老写法 JSON
    assert results[1]["is_error"] and "不许写 .bat" in results[1]["content"]  # 新写法 permissionDecision
    assert not results[2]["is_error"] and (settings.workdir / "ok.txt").read_text(encoding="utf-8") == "好"
    assert not (settings.workdir / "run.bat").exists()
    calls = _log(log)
    assert calls[0]["tool_name"] == "Bash" and calls[0]["tool_input"]["command"] == "rm -rf build"  # Claude 的工具名
    assert calls[1]["tool_name"] == "Write"
    assert calls[1]["tool_input"]["file_path"] == str((settings.workdir / "run.bat").resolve())  # 绝对路径的 file_path
    assert "path" not in calls[1]["tool_input"]
    assert calls[0]["hook_event_name"] == "PreToolUse" and calls[0]["cwd"] == str(settings.workdir)
    assert calls[0]["permission_mode"] == "bypassPermissions" and calls[0]["_env_project_dir"] == str(settings.workdir)


def test_exit_2_blocks_before_asking(tmp_path: Path) -> None:
    settings, _ = _setup(tmp_path, {"PreToolUse": [("Bash", "exit2")]})
    settings = settings.model_copy(update={"permission_mode": "ask"})
    agent, model = _agent(settings, [[("tool", "bash", {"command": "echo hi"})], ["好"]])
    asked: list[str] = []
    agent.run("跑一下", lambda e: None, lambda t, a: asked.append(t.name) or "once")  # type: ignore[func-returns-value]
    result = model.calls[1][-1]["content"][0]
    assert result["is_error"] and "退出码 2 拦下：危险命令" in result["content"]
    assert asked == []  # 钩子先于确认框：要被拦下的调用，不先问用户


def test_matcher_and_post_tool(tmp_path: Path) -> None:
    settings, log = _setup(tmp_path, {"PreToolUse": [("Bash", "guard")], "PostToolUse": [("Write|Edit", "post")]})
    agent, model = _agent(settings, [[("tool", "write_file", {"path": "a.py", "content": "x = 1\n"})], ["好"]])
    agent.run("写个文件", lambda e: None, lambda t, a: "once")
    result = model.calls[1][-1]["content"][0]
    assert "格式检查：通过" in result["content"]  # additionalContext 交给模型
    calls = _log(log)
    assert [c["hook_event_name"] for c in calls] == ["PostToolUse"]  # Bash 的钩子对 Write 不跑
    assert "a.py" in calls[0]["tool_response"]["content"]


def test_user_prompt_submit(tmp_path: Path) -> None:
    settings, log = _setup(tmp_path, {"UserPromptSubmit": [("", "prompt")]})
    agent, model = _agent(settings, [["好"]])
    agent.run("看下代码", lambda e: None, lambda t, a: "once")
    assert "当前分支是 main" in str(model.calls[0][-1]["content"])  # 纯文字输出：补充上下文
    done = agent.run("我的密码是 123", lambda e: None, lambda t, a: "once")
    assert len(model.calls) == 1 and done.rounds == 0  # 被拦下：没发给模型
    assert _log(log)[0]["prompt"] == "看下代码"


def test_stop_continue_once(tmp_path: Path) -> None:
    settings, log = _setup(tmp_path, {"Stop": [("", "stop")]})
    agent, model = _agent(settings, [["Here is the answer in English."], ["这是中文的回答。"]])
    agent.run("问个问题", lambda e: None, lambda t, a: "once")
    assert len(model.calls) == 2 and "请用中文重新回答" in str(model.calls[1][-1]["content"])  # 要求继续
    calls = _log(log)
    assert [c["stop_hook_active"] for c in calls] == [False, True]  # 第二次知道是继续之后又停下：钩子据此放行
    assert calls[0]["last_assistant_message"] == "Here is the answer in English."


def test_hook_dataclass_matches_claude_names() -> None:
    hook = Hook("pre_tool", "Bash|PowerShell", "x", style="claude")
    assert hook.matches("bash") and not hook.matches("read_file")
    assert Hook("pre_tool", "mcp__github__.*", "x", style="claude").matches("mcp__github__create_issue")


def test_skip_list(tmp_path: Path) -> None:
    settings, _ = _setup(tmp_path, {"SessionStart": [("", "x")], "Stop": [("", "stop")]})
    user, _ = load_hooks(settings.home, settings.workdir, skip=["hook.py"])
    assert user == []  # 命令里含 hook.py 的 Claude 钩子都跳过（给 Claude Code 自己用的集成钩子）
    user, _ = load_hooks(settings.home, settings.workdir, skip=["别的"])
    assert len(user) == 2

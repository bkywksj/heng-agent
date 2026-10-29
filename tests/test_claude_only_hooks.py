"""专给 Claude Code 用的集成钩子（智码的会话记录器、额度保护、回复语言守卫）在 heng-agent 里不跑，安全防护类照跑。"""

from __future__ import annotations

import json
from pathlib import Path

from heng_agent.hooks import CLAUDE_ONLY_HOOKS, load_hooks


def _cmd(name: str) -> dict[str, str]:
    # 智码真实注册的钩子命令形如 node "C:/Users/x/.claude/hooks/aicoder-quota-guard.cjs"（取自本机 settings.json）
    return {"type": "command", "command": f'node "C:/Users/x/.claude/hooks/{name}"'}


def test_claude_only_hooks_are_skipped(tmp_path: Path, _isolate_claude_home: Path) -> None:
    hooks = {
        "SessionStart": [{"hooks": [_cmd(".aicoder-claude-session-recorder.js")]}],
        "UserPromptSubmit": [{"hooks": [_cmd("aicoder-quota-guard.cjs")]}],
        "Stop": [{"hooks": [_cmd("aicoder-reply-language-guard.cjs")]}],
        "PreToolUse": [
            {
                "matcher": "Bash|PowerShell",
                "hooks": [_cmd("aicoder-host-guard.cjs"), _cmd("dangerous-command-guard.cjs")],
            }
        ],
    }
    (_isolate_claude_home / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    user, _ = load_hooks(tmp_path / "h", tmp_path)
    commands = " ".join(h.command for h in user)
    for name in CLAUDE_ONLY_HOOKS:
        assert name not in commands  # 记会话、按 Claude 额度拦提问、读 Claude 对话记录的：不跑
    assert "aicoder-host-guard" in commands and "dangerous-command-guard" in commands  # 安全防护照跑
    # 用户自己配的跳过照样叠加
    user, _ = load_hooks(tmp_path / "h", tmp_path, skip=["dangerous-command-guard"])
    assert "dangerous-command-guard" not in " ".join(h.command for h in user)

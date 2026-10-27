"""兼容其他编码工具的配置：Claude Code（CLAUDE.md、.claude/skills、.claude/agents、.mcp.json）与 AGENTS.md 开放标准。

用户从别的工具转过来时，项目里本来就带着这些配置；直接读进来，零迁移成本。几条原则：
- heng-agent 自己的配置优先：同名时 .heng-agent/ 的覆盖 .claude/ 的；
- 能执行代码的（项目里的 MCP 服务、钩子）照样要用户确认信任；
- 读了哪些要让用户看得见（欢迎页、/doctor），不在后台悄悄生效。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 项目根目录的说明文件，都读（内容一样的只算一份，见 context）
PROJECT_MEMORY_NAMES = ("HENG.md", "AGENTS.md", "CLAUDE.md", "CLAUDE.local.md")
#: 同一级里的优先级，从低到高（系统提示词按这个顺序排，冲突时以后面的为准）：CLAUDE.md 是写给 Claude Code 的，
#: AGENTS.md 是跨工具的标准，CLAUDE.local.md 是个人本地的补充，HENG.md 是专门写给 heng-agent 的、最具体
MEMORY_PRIORITY = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md", "HENG.md")
#: 子目录的说明文件：模型第一次碰到那个目录的文件时附上；顺序同上，从低到高
NESTED_MEMORY_NAMES = ("CLAUDE.md", "AGENTS.md", "HENG.md")

#: Claude Code 的工具名 → heng-agent 的工具名（.claude/agents 里 tools 字段用的是前者）
CLAUDE_TOOLS = {
    "read": "read_file", "write": "write_file", "edit": "edit_file", "multiedit": "edit_file",
    "bash": "bash", "grep": "grep", "glob": "glob", "ls": "glob", "webfetch": "web_fetch",
}  # fmt: skip


def claude_home() -> Path:
    """Claude Code 的用户配置目录：和它一样认 CLAUDE_CONFIG_DIR 环境变量，没设就是 ~/.claude。"""
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(custom) if custom else Path.home() / ".claude"


def project_memory_file(workdir: Path) -> Path:
    """往项目说明里写东西（# 快速记忆、/init）时写哪个文件：已有 AGENTS.md、HENG.md、CLAUDE.md 就写现有的那个
    （别另起一份），都没有就新建 AGENTS.md——它是跨工具的开放标准，别的编码工具也读得到。"""
    for name in ("AGENTS.md", "HENG.md", "CLAUDE.md"):
        if (workdir / name).is_file():
            return workdir / name
    return workdir / "AGENTS.md"


#: Claude Code 权限规则里的工具 → heng-agent 的工具。Edit 规则在 Claude 里管所有改文件的工具；
#: PowerShell 是 Windows 上的 Claude Code 单独的命令工具，heng-agent 的命令都走 bash 工具
CLAUDE_RULE_TOOLS = {
    "bash": ("bash",), "powershell": ("bash",), "read": ("read_file",), "edit": ("edit_file", "write_file"),
    "multiedit": ("edit_file",), "write": ("write_file",), "glob": ("glob",), "grep": ("grep",),
    "webfetch": ("web_fetch",),
}  # fmt: skip


def convert_claude_rule(rule: str) -> list[str] | None:
    """把一条 Claude Code 的权限规则翻成 heng-agent 的写法；对应不上（如 WebSearch、Task）返回 None。

    `Bash(npm run test:*)` → `bash(npm run test*)`（`:*` 是 Claude 的前缀匹配）；`Read(./.env)` → `read_file(.env)`；
    `Edit(src/**)` → `edit_file(src/**)` 与 `write_file(src/**)`；`WebFetch(domain:x.com)` → 只认这个域名的网址；
    `mcp__服务` → `mcp__服务__*`，`mcp__服务__工具` 原样。
    """
    rule = rule.strip()
    if rule.startswith("mcp__"):
        return [rule if rule.count("__") >= 2 else f"{rule}__*"]
    name, paren, rest = rule.partition("(")
    key = name.strip().lower()
    targets = CLAUDE_RULE_TOOLS.get(key)
    if targets is None:
        return None
    if not paren:
        return list(targets)
    pattern = rest[:-1] if rest.endswith(")") else rest
    if key in ("bash", "powershell"):
        pattern = pattern[:-2] + "*" if pattern.endswith(":*") else pattern
    elif key == "webfetch":
        if not pattern.startswith("domain:"):
            return None
        domain = pattern.removeprefix("domain:")
        return [f"web_fetch(*://{domain}/*)", f"web_fetch(*://{domain})"]
    else:
        pattern = pattern.removeprefix("./")  # 路径：Claude 的 ./ 是相对项目根
    return [f"{tool}({pattern})" for tool in targets]


#: heng-agent 的工具名 → Claude Code 的（给 Claude 钩子看）。钩子脚本常写死 tool_name === 'Bash' 之类，
#: 必须报 Claude 的名字它们才认。bash 工具优先用 Git Bash 跑，报 Bash
HENG_TO_CLAUDE = {
    "bash": "Bash", "read_file": "Read", "write_file": "Write", "edit_file": "Edit", "glob": "Glob",
    "grep": "Grep", "web_fetch": "WebFetch", "todo": "TodoWrite", "task": "Task", "bash_output": "BashOutput",
    "kill_task": "KillShell",
}  # fmt: skip
#: Claude Code 的钩子事件 → heng-agent 的（其余的如 Notification、SubagentStop、PreCompact 暂不支持）
CLAUDE_EVENTS = {
    "PreToolUse": "pre_tool", "PostToolUse": "post_tool", "UserPromptSubmit": "user_prompt_submit",
    "Stop": "stop", "SessionStart": "session_start",
}  # fmt: skip

"""自定义子智能体（角色）：把「测试员」「审查员」这类专门的活儿交给带固定提示词与工具的子智能体。

位置：项目 `.heng-agent/agents/<名字>.md`、用户 `~/.heng-agent/agents/<名字>.md`，同名时项目的优先。

    ---
    description: 跑测试并分析失败原因（模型据此决定派不派它）
    tools: read_file, glob, grep, bash
    ---
    你是测试员。先跑项目的测试……（正文就是这个角色的提示词）

tools 不写就是只读（读文件、找文件、搜内容）。只读的角色不用确认、可以并行；带改文件或跑命令工具的，
按主智能体的权限模式来（该问用户的照样问），而且一个个来——并行的话确认框会互相打架、同时写文件会冲突。
子智能体不能再派子智能体，也不接 MCP 工具（要用就让主智能体用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from heng_agent.core.compat import CLAUDE_TOOLS, claude_home
from heng_agent.tools.skill import parse_frontmatter

READ_ONLY_TOOLS = ("read_file", "glob", "grep")
#: 子智能体能用的工具（内置工具里除了派子智能体、任务清单、技能这些主智能体专用的）
ALLOWED_TOOLS = ("read_file", "glob", "grep", "write_file", "edit_file", "bash", "web_fetch")

TEMPLATE = """---
description: {description}
tools: read_file, glob, grep
---
你是{name}。在这里写这个角色要怎么做事：先做什么、注意什么、最后交回什么样的结论。

（tools 可选：read_file, glob, grep, write_file, edit_file, bash, web_fetch。不写就是只读。
带 write_file / edit_file / bash 的角色按主智能体的权限模式来，改文件、跑命令前照样会问你。）
"""


@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    tools: tuple[str, ...]
    prompt: str
    path: Path
    unknown_tools: tuple[str, ...] = ()  # 写了但不认识 / 不允许的工具（忽略，/agents 里提示）

    @property
    def read_only(self) -> bool:
        return all(t in READ_ONLY_TOOLS for t in self.tools)


def _parse_tools(raw: str, *, default: tuple[str, ...] = READ_ONLY_TOOLS) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """tools 字段 → (认得的工具, 不认识 / 不允许的)。heng-agent 的工具名（read_file……）与 Claude Code 的（Read、Bash……）
    都认；实际的 .claude/agents 里两种混着写的都有。一个也没写 → default。"""
    names = [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]
    if not names:
        return default, ()
    known: list[str] = []
    unknown: list[str] = []
    for name in names:
        mapped = name if name in ALLOWED_TOOLS else CLAUDE_TOOLS.get(name.lower())
        (known if mapped else unknown).append(mapped or name)
    return tuple(dict.fromkeys(known)) or READ_ONLY_TOOLS, tuple(unknown)


def discover_agents(home: Path, workdir: Path, *, claude_user: bool = True) -> dict[str, AgentDef]:
    """角色：heng-agent 自己的与 Claude Code 的（.claude/agents）都读。优先级低的先读、后读的覆盖同名：
    Claude 用户级 → heng 用户级 → Claude 项目级 → heng 项目级。

    两边不写 tools 时的意思不一样：heng-agent 的当作只读；Claude Code 的照它的规则继承全部工具
    （带改动工具的角色照样按权限模式向用户确认）。model 字段忽略，用当前模型。
    """
    agents: dict[str, AgentDef] = {}
    roots = ((claude_home() / "agents", True), (home / "agents", False),
             (workdir / ".claude" / "agents", True), (workdir / ".heng-agent" / "agents", False))  # fmt: skip
    for root, from_claude in roots[0 if claude_user else 1 :]:  # claude_user=False：不读 ~/.claude/agents
        if not root.is_dir():
            continue
        for md in sorted(root.glob("*.md")):
            meta, body = parse_frontmatter(md.read_text(encoding="utf-8-sig", errors="replace"))
            default = ALLOWED_TOOLS if from_claude else READ_ONLY_TOOLS
            tools, unknown = _parse_tools(str(meta.get("tools") or ""), default=default)
            name = str(meta.get("name") or md.stem)
            first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
            agents[name] = AgentDef(name, str(meta.get("description") or first[:60]), tools, body.strip(), md, unknown)
    return agents


def create_agent(
    workdir: Path, name: str, description: str = "（一句话说清这个角色做什么，模型据此决定派不派它）"
) -> Path:
    """在项目里新建一个角色文件（已存在就不动），返回路径。"""
    folder = workdir / ".heng-agent" / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    if not path.exists():
        path.write_text(TEMPLATE.format(name=name, description=description), encoding="utf-8")
    return path

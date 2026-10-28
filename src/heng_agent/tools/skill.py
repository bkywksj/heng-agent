"""技能：某类任务的做法说明（SKILL.md），按需加载。

位置：项目 `.heng-agent/skills/<名字>/SKILL.md`、用户 `~/.heng-agent/skills/<名字>/SKILL.md`，同名时项目的优先。
两步加载：系统提示词里只列「名字 + 一句话说明」，模型觉得用得上再调 skill 工具读全文——装再多技能也不挤占上下文。
技能目录里的其他文件（模板、示例、清单）用 skill 工具的 file 参数读：用户级技能在工作目录之外，read_file 读不到。

SKILL.md 开头可以有一段 frontmatter：

    ---
    name: commit
    description: 写提交信息与提交前检查的规矩
    ---
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from heng_agent.core.compat import claude_home
from heng_agent.core.types import ToolResult
from heng_agent.tools.base import Tool, ToolContext

MAX_SKILL_CHARS = 30_000


def _block(lines: list[str], style: str) -> str:
    """YAML 块文本：| 保留换行；> 把相邻的行用空格连起来、空行变成换行。先去掉公共缩进。"""
    indent = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
    body = [ln[indent:].rstrip() for ln in lines]
    while body and not body[-1]:
        body.pop()
    if style.startswith("|"):
        return "\n".join(body)
    paragraphs, current = [], []
    for ln in body:
        if ln:
            current.append(ln.strip())
        else:
            paragraphs.append(" ".join(current))
            current = []
    paragraphs.append(" ".join(current))
    return "\n".join(p for p in paragraphs if p)


def _parse_meta(lines: list[str]) -> dict[str, str]:
    """frontmatter 里技能、子智能体、命令实际用到的 YAML 写法：
    `键: 值`；`键: |` / `键: >` 加缩进的多行文本；`键:` 加 `- 项` 的列表（拼成「a, b」）；
    值后面跟着缩进的续行（拼成一行）；`#` 开头的注释。

    以前只认单行的 `键: 值`：`description: |` 只读到一个「|」，续行里带冒号的还被拆成一堆乱七八糟的字段——
    技能在系统提示词里的说明就只剩一个「|」，模型不知道它是干什么的（ruoyi-plus-uniapp 的 60 个技能全是这种写法）。
    """
    meta: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#") or line[:1] in (" ", "\t"):
            continue  # 空行、注释、没归属的缩进行
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        nested: list[str] = []  # 跟在这个键后面的缩进行（多行文本、列表、续行）
        while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
            nested.append(lines[i])
            i += 1
        if value[:1] in ("|", ">"):
            meta[key.strip()] = _block(nested, value)
        elif not value and any(ln.strip().startswith("- ") for ln in nested):
            items = [ln.strip()[2:].strip().strip("\"'") for ln in nested if ln.strip().startswith("- ")]
            meta[key.strip()] = ", ".join(items)
        else:
            continuation = " ".join(ln.strip() for ln in nested if ln.strip())
            meta[key.strip()] = " ".join(p for p in (value.strip("\"'"), continuation) if p)
    return meta


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析开头的 `---` 包起来的 frontmatter，返回 (字段, 正文)。没有 frontmatter 就返回空字段与原文。"""
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            return _parse_meta(lines[1:end]), "\n".join(lines[end + 1 :]).lstrip("\n")
    return {}, text


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # SKILL.md

    @property
    def dir(self) -> Path:
        return self.path.parent


#: 随安装包带的内置技能（heng_agent/skills/<名字>/SKILL.md）：优先级最低，用户、项目里同名的会覆盖它们
BUILTIN_SKILLS = Path(__file__).resolve().parent.parent / "skills"


def discover_skills(home: Path, workdir: Path, *, claude_user: bool = True) -> dict[str, Skill]:
    """技能：内置的、heng-agent 自己的与 Claude Code 的（格式一样，都是 名字/SKILL.md）都读。

    优先级低的先读、后读的覆盖同名：内置 → Claude 用户级 → heng 用户级 → Claude 项目级 → heng 项目级——
    项目的压过用户的，同一级里 heng-agent 自己的压过 Claude 的，内置的可以被任何一级改写。
    claude_user=False 时不读 ~/.claude/skills。
    """
    skills: dict[str, Skill] = {}
    roots = [BUILTIN_SKILLS]
    if claude_user:
        roots.append(claude_home() / "skills")
    roots += [home / "skills", workdir / ".claude" / "skills", workdir / ".heng-agent" / "skills"]
    for root in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            meta, body = parse_frontmatter(skill_md.read_text(encoding="utf-8-sig", errors="replace"))
            name = meta.get("name") or skill_md.parent.name
            first_line = next((line.strip("# ").strip() for line in body.splitlines() if line.strip()), "")
            skills[name] = Skill(name, meta.get("description") or first_line[:80], skill_md)
    return skills


def skill_source(skill: Skill, home: Path, workdir: Path) -> str:
    """技能从哪里来（/skills 里显示）：内置 / 用户 / 项目 / Claude 用户 / Claude 项目。"""
    # 解析前后都看：技能目录常是链接（junction / 符号链接），解析后就不在原来的目录下了
    paths = {skill.path.absolute(), skill.path.resolve()}
    places = [(BUILTIN_SKILLS, "内置"), (workdir / ".heng-agent" / "skills", "项目"),
              (workdir / ".claude" / "skills", "Claude 项目"), (home / "skills", "用户"),
              (claude_home() / "skills", "Claude 用户")]  # fmt: skip
    for root, label in places:
        roots = {root.absolute(), root.resolve()}
        if any(r in p.parents for p in paths for r in roots):
            return label
    return "其他"


class SkillTool(Tool):
    name = "skill"
    description = (
        "读取一个技能的完整说明（技能清单见系统提示词）。任务和某个技能的说明对得上时，先读它再动手。"
        "技能目录里的其他文件用 file 参数读。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名"},
            "file": {"type": "string", "description": "可选：技能目录里的其他文件（相对路径）"},
        },
        "required": ["name"],
    }

    def __init__(self, skills: dict[str, Skill]) -> None:
        self.skills = skills

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('name')}{' / ' + args['file'] if args.get('file') else ''}"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        skill = self.skills.get(str(args["name"]))
        if skill is None:
            return ToolResult(f"没有叫 {args['name']} 的技能，可用的有：{', '.join(self.skills)}", is_error=True)
        target = skill.path
        if args.get("file"):
            target = (skill.dir / str(args["file"])).resolve()
            if skill.dir.resolve() not in target.parents:  # 只能读技能自己目录里的文件
                return ToolResult("file 必须在技能目录之内", is_error=True)
            if not target.is_file():
                others = [p.relative_to(skill.dir).as_posix() for p in skill.dir.rglob("*") if p.is_file()]
                return ToolResult(f"技能目录里没有 {args['file']}，有：{', '.join(others)}", is_error=True)
        text = target.read_text(encoding="utf-8-sig", errors="replace")
        if target == skill.path:
            text = parse_frontmatter(text)[1]
            others = [
                p.relative_to(skill.dir).as_posix() for p in skill.dir.rglob("*") if p.is_file() and p != skill.path
            ]
            if others:
                text += f"\n\n（技能目录里还有：{', '.join(others)}，需要时用 file 参数读）"
        if len(text) > MAX_SKILL_CHARS:
            text = text[:MAX_SKILL_CHARS] + "\n…（太长，后面截掉了）"
        return ToolResult(text)

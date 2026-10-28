"""内置技能：随安装包带、优先级最低（用户、项目里同名的会替换）、/skills 按来源分组显示。"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.tools.skill import discover_skills, parse_frontmatter, skill_source
from heng_agent.ui.repl import Repl

BUILTIN = {"debugging", "writing-tests", "refactoring", "codebase-tour", "security-check"}


def test_builtin_skills_are_well_formed(real_builtin_skills: Path) -> None:
    dirs = {p.name for p in real_builtin_skills.iterdir() if (p / "SKILL.md").is_file()}
    assert dirs == BUILTIN
    for name in BUILTIN:
        meta, body = parse_frontmatter((real_builtin_skills / name / "SKILL.md").read_text(encoding="utf-8"))
        assert meta.get("name") == name  # 名字与目录一致
        assert "触发场景" in str(meta.get("description"))  # 模型靠它决定什么时候用
        assert len(body.strip()) > 300, name  # 有实在的内容


def test_found_and_overridable(tmp_path: Path, real_builtin_skills: Path) -> None:
    home, work = tmp_path / "h", tmp_path / "w"
    work.mkdir()
    skills = discover_skills(home, work)
    assert set(skills) >= BUILTIN
    assert skill_source(skills["debugging"], home, work) == "内置"
    custom = work / ".heng-agent" / "skills" / "debugging" / "SKILL.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("---\nname: debugging\ndescription: 我们团队的排查规范\n---\n先看监控。", encoding="utf-8")
    skills = discover_skills(home, work)
    assert skills["debugging"].description == "我们团队的排查规范"  # 项目里同名的替换内置的
    assert skill_source(skills["debugging"], home, work) == "项目"
    assert skill_source(skills["refactoring"], home, work) == "内置"


def test_listed_in_prompt_and_skills_command(tmp_path: Path, real_builtin_skills: Path) -> None:
    agent = Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([]))
    assert all(name in agent.system for name in BUILTIN)  # 系统提示词里列出，模型按需调用
    console = Console(record=True, width=160, force_terminal=False)
    Repl(agent, console=console).handle_command("/skills")
    shown = console.export_text()
    assert "内置" in shown and "debugging" in shown and "同名就会替换它" in shown


def test_packaged() -> None:
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert '"skills/*/*.md"' in pyproject  # 不声明就进不了安装包（以前内置提示词漏打包，装好就崩）


# ---------- frontmatter 的 YAML 写法（以前只认单行，description: | 只读到一个「|」） ----------


def test_frontmatter_block_and_list_styles() -> None:
    text = "\r\n".join([
        "---",
        "name: \"git-workflow\"",
        "# 注释不算字段",
        "description: |",
        "  Git 提交规范。",
        "  触发场景：提交 / 推送 / 分支",
        "",
        "  （与 release 区别：不管发版）",
        "summary: >",
        "  第一行",
        "  接着第一行",
        "",
        "  第二段",
        "tools:",
        "  - Read",
        "  - 'Bash'",
        "model: inherit",
        "---",
        "正文第一行",
    ])  # fmt: skip
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "git-workflow"
    assert meta["description"] == "Git 提交规范。\n触发场景：提交 / 推送 / 分支\n\n（与 release 区别：不管发版）"
    assert meta["summary"] == "第一行 接着第一行\n第二段"  # > 折行：同段连成一行，空行分段
    assert meta["tools"] == "Read, Bash"  # 列表拼成「a, b」，子智能体的 tools 字段就能用
    assert meta["model"] == "inherit" and body == "正文第一行"
    assert "触发场景" not in meta and "（与 release 区别" not in meta  # 续行里带冒号的不再被拆成乱字段


def test_frontmatter_plain_value_with_continuation() -> None:
    # Claude 子智能体常见：描述写成一行，后面跟着缩进的续行，续行里有「Context: …」这种带冒号的句子
    text = "---\nname: dev-planner\ndescription: 规划开发任务。\n  Context: 用户要做一个大功能\n  Examples: 拆成小步\n"
    meta, _ = parse_frontmatter(text + "color: blue\n---\n正文")
    assert meta["description"] == "规划开发任务。 Context: 用户要做一个大功能 Examples: 拆成小步"
    assert set(meta) == {"name", "description", "color"}  # 续行里的「Context:」不再被当成字段


def test_skill_list_in_prompt_is_one_line_each() -> None:
    from heng_agent.context import SKILL_SUMMARY_CHARS, skill_summary

    assert skill_summary("第一行\n  第二行\n\n第三段") == "第一行 第二行 第三段"  # 多行压成一行，清单格式不乱
    long = skill_summary("很长" * 500)
    assert len(long) == SKILL_SUMMARY_CHARS and long.endswith("…")  # 太长截断：几十个技能也不白占上下文

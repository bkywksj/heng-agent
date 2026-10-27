"""兼容 Claude Code 与 AGENTS.md：说明文件（去重、@引用、子目录）、.claude/skills、.claude/agents、.mcp.json。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.agents import ALLOWED_TOOLS, discover_agents
from heng_agent.context import build_system_prompt, load_memories, nested_memory
from heng_agent.core.compat import project_memory_file
from heng_agent.core.config import Settings
from heng_agent.mcp import load_servers
from heng_agent.tools.skill import discover_skills
from heng_agent.ui.repl import Repl


def claude_dir() -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"])  # conftest 指到的空临时目录


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    work = tmp_path / "proj"
    work.mkdir()
    return Settings(workdir=work, home=tmp_path / "heng-home")


# ---------- 说明文件 ----------


def test_all_memory_files_loaded_and_deduped(settings: Settings) -> None:
    work = settings.workdir
    write(work / "AGENTS.md", "构建用 pnpm build")
    write(work / "CLAUDE.md", "构建用 pnpm build")  # 和 AGENTS.md 一样（常见：一份复制成两个名字）
    write(work / "CLAUDE.local.md", "我本机的数据库端口是 5433")
    write(claude_dir() / "CLAUDE.md", "回答用中文")
    system = build_system_prompt(settings)
    assert system.count("构建用 pnpm build") == 1  # 内容一样只算一份
    assert "我本机的数据库端口是 5433" in system and "回答用中文" in system
    assert "用户记忆（Claude Code）" in system


def test_claude_imports(settings: Settings) -> None:
    work = settings.workdir
    write(work / "docs" / "规范.md", "接口一律返回 {code, data}，见 @循环.md")
    write(work / "docs" / "循环.md", "回到 @规范.md（互相引用不能死循环）")
    write(work / "CLAUDE.md", "\n".join([
        "约定见 @docs/规范.md。",
        "有问题找 admin@example.com",  # 邮箱不是引用
        "```",
        "@docs/规范.md",  # 代码块里不展开
        "```",
        "不存在的 @docs/没有这个.md 原样留着",
    ]))  # fmt: skip
    (_, block), *_ = load_memories(settings)
    assert block.count("接口一律返回") == 1 and "回到" in block  # 嵌套引用展开、互相引用只展开一次
    assert "（以下引用自 docs/规范.md）" in block and "admin@example.com" in block


def test_nested_memory_recognizes_all_names(settings: Settings) -> None:
    work = settings.workdir
    write(work / "api" / "AGENTS.md", "接口层约定")
    write(work / "api" / "CLAUDE.md", "接口层约定")  # 同样内容
    write(work / "api" / "v2" / "CLAUDE.md", "v2 用新鉴权")
    found = nested_memory(work / "api" / "v2" / "user.py", work, set())
    assert [text for _, text in found] == ["接口层约定", "v2 用新鉴权"]


def test_which_file_to_write(tmp_path: Path) -> None:
    assert project_memory_file(tmp_path).name == "AGENTS.md"  # 都没有：新建跨工具通用的 AGENTS.md
    write(tmp_path / "CLAUDE.md", "x")
    assert project_memory_file(tmp_path).name == "CLAUDE.md"  # 已有就写现有的，不另起一份
    write(tmp_path / "AGENTS.md", "x")
    assert project_memory_file(tmp_path).name == "AGENTS.md"


# ---------- 技能、子智能体 ----------


def test_claude_skills(settings: Settings) -> None:
    write(claude_dir() / "skills" / "部署" / "SKILL.md", "---\ndescription: Claude 用户级部署\n---\n正文")
    write(claude_dir() / "skills" / "打包" / "SKILL.md", "---\ndescription: Claude 用户级打包\n---\n正文")
    write(settings.workdir / ".claude" / "skills" / "测试" / "SKILL.md", "---\ndescription: Claude 项目级\n---\n正文")
    write(settings.workdir / ".heng-agent" / "skills" / "打包" / "SKILL.md", "---\ndescription: heng 的打包\n---\n正文")
    skills = discover_skills(settings.home, settings.workdir)
    assert set(skills) == {"部署", "打包", "测试"}
    assert skills["打包"].description == "heng 的打包"  # 同名：heng-agent 自己的优先


def test_claude_agents_real_world_formats(settings: Settings) -> None:
    folder = claude_dir() / "agents"
    # 真实配置里见到的写法：CRLF 换行、Claude 工具名与别的工具名混写、不写 tools、带 model / color
    write(
        folder / "sketch.md",
        "---\r\nname: ui-sketcher\r\ndescription: 画界面草图\r\n"
        "tools: Bash, Glob, Read, WebFetch, TodoWrite, WebSearch\r\nmodel: inherit\r\ncolor: purple\r\n---\r\n正文\r\n",
    )
    write(folder / "review.md", "---\nname: code-reviewer\ndescription: 审查代码\nmodel: opus\n---\n你是审查员")
    write(folder / "bug.md", "---\nname: bug-analyzer\ndescription: 查 bug\n"
          "tools: read_file, write_file, run_bash_command, grep\n---\n正文")  # fmt: skip
    agents = discover_agents(settings.home, settings.workdir)
    sketch = agents["ui-sketcher"]  # 名字取 frontmatter 的 name，不是文件名
    assert sketch.tools == ("bash", "glob", "read_file", "web_fetch") and sketch.unknown_tools == (
        "TodoWrite",
        "WebSearch",
    )
    assert agents["code-reviewer"].tools == ALLOWED_TOOLS and not agents["code-reviewer"].read_only  # 不写 = 继承全部
    assert agents["bug-analyzer"].tools == ("read_file", "write_file", "grep")
    assert agents["bug-analyzer"].unknown_tools == ("run_bash_command",)


# ---------- .mcp.json ----------


def test_mcp_json(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_TOKEN", "t-123")
    monkeypatch.delenv("NOT_SET", raising=False)
    write(
        settings.workdir / ".mcp.json",
        """{"mcpServers": {
        "docs": {"type": "http", "url": "${DOCS_URL:-https://docs.example/mcp}",
                 "headers": {"Authorization": "Bearer ${TEAM_TOKEN}"}},
        "local": {"command": "tool", "args": ["--key", "${NOT_SET}"]},
        "both": {"command": "from-claude"}
    }}""",
    )
    write(settings.workdir / ".heng-agent" / "config.json", '{"mcp_servers": {"both": {"command": "from-heng"}}}')
    _, project = load_servers(settings.home, settings.workdir)
    by_name = {s.name: s for s in project}
    assert by_name["docs"].url == "https://docs.example/mcp"  # ${变量:-默认值}
    assert by_name["docs"].headers == {"Authorization": "Bearer t-123"}  # ${变量}
    assert by_name["local"].args == ["--key", ""]  # 没设也没默认值：留空
    assert by_name["both"].command == "from-heng"  # 同名：heng-agent 自己的优先
    assert all(s.source == "project" for s in project)  # 项目级：要用户确认信任才启动


# ---------- 让用户看得见 ----------


def test_banner_shows_what_was_loaded(settings: Settings) -> None:
    write(settings.workdir / "AGENTS.md", "说明")
    write(claude_dir() / "CLAUDE.md", "我的习惯")
    write(claude_dir() / "agents" / "a.md", "---\nname: 审查员\ndescription: 审查\n---\n正文")
    write(settings.workdir / ".claude" / "skills" / "部署" / "SKILL.md", "---\ndescription: 部署\n---\n正文")
    write(settings.workdir / ".mcp.json", '{"mcpServers": {}}')
    console = Console(record=True, width=200, force_terminal=False)
    Repl(Agent(settings, FakeModel([])), console=console).banner()
    shown = console.export_text()
    line = next(ln for ln in shown.splitlines() if "兼容加载" in ln)
    for item in ("AGENTS.md", "~/.claude/CLAUDE.md", "Claude 技能 1 个", "Claude 子智能体 1 个", ".mcp.json"):
        assert item in line, item
    assert "/init" not in shown  # 已经有 AGENTS.md：不再提示 /init


def test_claude_user_config_can_be_turned_off(settings: Settings) -> None:
    write(claude_dir() / "CLAUDE.md", "给 Claude Code 写的全局规则")
    write(claude_dir() / "skills" / "部署" / "SKILL.md", "---\ndescription: 部署\n---\n正文")
    write(claude_dir() / "agents" / "a.md", "---\nname: 审查员\ndescription: 审查\n---\n正文")
    write(settings.workdir / ".claude" / "skills" / "项目技能" / "SKILL.md", "---\ndescription: 项目的\n---\n正文")
    write(settings.workdir / "CLAUDE.md", "项目约定")
    on = Agent(settings, FakeModel([]))
    assert "给 Claude Code 写的全局规则" in on.system and "部署" in on.skills and "审查员" in on.agent_defs
    off = Agent(settings.model_copy(update={"claude_user_config": False}), FakeModel([]))
    assert "给 Claude Code 写的全局规则" not in off.system  # 用户级的不读了
    assert "部署" not in off.skills and "审查员" not in off.agent_defs
    assert "项目约定" in off.system and "项目技能" in off.skills  # 项目里的照读


# ---------- 第二批：自定义命令 ----------


def test_claude_commands(settings: Settings) -> None:
    from heng_agent.commands import discover_commands

    write(claude_dir() / "commands" / "提交.md", "---\ndescription: 用户级提交\n---\n提交 $ARGUMENTS")
    write(settings.workdir / ".claude" / "commands" / "前端" / "组件.md",
          "---\ndescription: 建组件\nargument-hint: [名字] [目录]\n---\n在 $2 建组件 $1")  # fmt: skip
    commands = discover_commands(settings.home, settings.workdir)
    assert commands["提交"].description == "用户级提交" and not commands["提交"].project
    component = commands["组件"]  # 子目录里的也认，名字取文件名
    assert component.hint == "[名字] [目录]" and component.project
    assert component.expand("Button src/ui") == "在 src/ui 建组件 Button"
    assert component.expand("Button") == "在  建组件 Button"  # 缺的位置参数留空
    off = discover_commands(settings.home, settings.workdir, claude_user=False)
    assert "提交" not in off and "组件" in off


def test_command_shell_snippets() -> None:
    from heng_agent.commands import Command

    command = Command("看提交", "", "最近的提交：!`git log -1 $1`\n请总结。", Path("x.md"))
    ran: list[str] = []

    def fake_shell(cmd: str) -> str:
        ran.append(cmd)
        return "abc123 修复 $1 的 bug"  # 输出里碰巧有 $1：不能被当成参数换掉

    text = command.expand("HEAD", fake_shell)
    assert ran == ["git log -1 HEAD"]  # 先填参数再跑
    assert "abc123 修复 $1 的 bug" in text and "$ git log -1 HEAD" in text
    assert "（没有运行：git log -1 HEAD）" in command.expand("HEAD", None)  # 不跑：原处注明


def test_project_command_shell_needs_consent(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    write(settings.workdir / ".claude" / "commands" / "状态.md", "看看：!`git status`")
    write(claude_dir() / "commands" / "我的.md", "看看：!`git status`")
    repl = Repl(Agent(settings, FakeModel([])), console=Console(record=True, force_terminal=False))
    monkeypatch.setattr(repl, "_choose", lambda title, options: "no")
    assert repl._command_shell(repl.commands["状态"]) is None  # 项目里的：用户说不跑就不跑
    assert repl._command_shell(repl.commands["我的"]) is not None  # 用户级的：自己写的，直接跑
    repl.agent.permissions.mode = "auto"
    assert repl._command_shell(repl.commands["状态"]) is not None  # auto 模式不问


# ---------- 第二批：权限规则 ----------


def test_convert_claude_rules() -> None:
    from heng_agent.core.compat import convert_claude_rule as conv

    assert conv("Bash(npm run test:*)") == ["bash(npm run test*)"]
    assert conv("PowerShell(Remove-Item -Recurse*)") == ["bash(Remove-Item -Recurse*)"]
    assert conv("Read(./.env)") == ["read_file(.env)"]
    assert conv("Edit(src/**)") == ["edit_file(src/**)", "write_file(src/**)"]
    assert conv("WebFetch(domain:docs.python.org)") == [
        "web_fetch(*://docs.python.org/*)",
        "web_fetch(*://docs.python.org)",
    ]
    assert conv("mcp__github") == ["mcp__github__*"] and conv("mcp__github__create_issue") == [
        "mcp__github__create_issue"
    ]
    assert conv("Bash") == ["bash"]
    assert conv("WebSearch") is None and conv("Task(x)") is None


def test_claude_settings_permissions(settings: Settings) -> None:
    from heng_agent import configfile

    write(claude_dir() / "settings.json", '{"permissions": {"allow": ["Bash(npm test:*)"], '
          '"deny": ["Bash(git push --force*)", "WebSearch"], "defaultMode": "auto"}}')  # fmt: skip
    write(settings.workdir / ".claude" / "settings.json",
          '{"permissions": {"allow": ["Bash(*)"], "deny": ["Read(./.env)"]}}')  # fmt: skip
    write(settings.workdir / ".claude" / "settings.local.json", "{这不是 JSON")  # 写坏了：跳过，不拦住启动
    allow, deny, ignored, unsupported = configfile.permission_rules(settings.home, settings.workdir)
    assert allow == ["bash(npm test*)"]
    assert deny == ["bash(git push --force*)", "read_file(.env)"]
    assert ignored == ["bash(*)"]  # 项目里的允许规则不采用：恶意仓库不能给自己放开命令
    assert unsupported == ["WebSearch"]
    allow, deny, _, _ = configfile.permission_rules(settings.home, settings.workdir, claude_user=False)
    assert allow == [] and deny == ["read_file(.env)"]  # 关掉用户级：~/.claude 的不读，项目的照读


def test_deny_rules_cannot_be_bypassed() -> None:
    from heng_agent.permissions import Rules
    from heng_agent.tools.files import ReadFile
    from heng_agent.tools.shell import Bash

    rules = Rules(deny=["bash(git push --force*)", "read_file(.env)"])
    bash, read = Bash(), ReadFile()
    for command in ("git push --force", "cd web && git push --force origin main", "echo 1; git push --force",
                    "npm test || git push --force"):  # fmt: skip
        assert rules.decide(bash, {"command": command}) == "deny", command  # 组合命令逐段检查
    assert rules.decide(bash, {"command": "git push origin main"}) is None
    for path in (".env", "./.env", ".\\.env", "E:\\项目\\.env", "src/../.env", "/home/me/proj/.env"):
        assert rules.decide(read, {"path": path}) == "deny", path  # 换个路径写法也拦得住
    assert rules.decide(read, {"path": ".env.example"}) is None


# ---------- 几份说明并存：优先级、工具名对照、总览 ----------


def test_memory_priority_order(settings: Settings) -> None:
    write(claude_dir() / "CLAUDE.md", "甲：用户级 Claude")
    for name, text in (("HENG.md", "戊：HENG"), ("AGENTS.md", "丙：AGENTS"), ("CLAUDE.md", "乙：项目 CLAUDE"),
                       ("CLAUDE.local.md", "丁：本地")):  # fmt: skip
        write(settings.workdir / name, text)
    system = build_system_prompt(settings)
    positions = [system.index(t) for t in ("甲：", "乙：", "丙：", "丁：", "戊：")]
    assert positions == sorted(positions)  # 从低到高：用户级 → CLAUDE.md → AGENTS.md → CLAUDE.local.md → HENG.md
    assert "内容冲突时以后面的为准" in system and "Bash / PowerShell → bash" in system


def test_preamble_only_when_needed(settings: Settings) -> None:
    from heng_agent.context import memory_preamble

    heng = settings.workdir / "HENG.md"
    assert memory_preamble([heng]) == ""  # 只有一份 HENG.md：既没冲突、也没别的工具的写法，不白占上下文
    two = memory_preamble([heng, settings.home / "HENG.md"])
    assert "优先级" in two and "→ bash" not in two  # 两份以上才写优先级
    foreign = memory_preamble([settings.workdir / "CLAUDE.md"])
    assert "→ read_file" in foreign and "优先级从低到高" not in foreign  # 有写给别的工具的才加对照


def test_memory_overview(settings: Settings) -> None:
    from heng_agent.context import estimate_tokens

    assert estimate_tokens("你好世界") == 4 and estimate_tokens("a" * 40) == 10
    write(settings.workdir / "CLAUDE.md", "一" * 6_000)
    write(settings.workdir / "AGENTS.md", "二" * 6_000)
    console = Console(record=True, width=200, force_terminal=False)
    repl = Repl(Agent(settings, FakeModel([])), console=console)
    repl.handle_command("/memory")
    shown = console.export_text()
    lines = shown.splitlines()
    first = next(i for i, ln in enumerate(lines) if "1." in ln and "CLAUDE.md" in ln)
    assert "AGENTS.md" in lines[first + 1]  # 按优先级从低到高列出
    assert "约 6.0k token" in shown or "约 6k token" in shown
    assert "说明文件偏大" in shown and "@AGENTS.md" in shown  # 合计超过 1 万 token：提醒，并给出合并的办法
    target = shown[shown.index("写到哪里") :].split("/memory edit user")[0]  # 路径长时这一行会折行：按文字取
    assert "AGENTS.md" in target  # # 快速记忆写进现有的 AGENTS.md

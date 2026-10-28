"""所有测试共用：把 Claude Code 的用户配置目录指到一个空的临时目录。

heng-agent 会读 ~/.claude 里的 CLAUDE.md、技能、子智能体（兼容 Claude Code）。不隔离的话，
测试结果会随跑测试那台机器的 Claude Code 配置而变。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_claude_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path_factory.mktemp("claude-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    # 测试从源码跑、会被当成开发版：没指定 home 的 Settings 会落到真实的 ~/.heng-agent-dev。也指到临时目录
    monkeypatch.setenv("HENG_AGENT_HOME", str(tmp_path_factory.mktemp("heng-home")))
    # 内置技能默认指到空目录：大多数测试要的是「只有它自己放的技能」，以后加内置技能也不连带改坏它们。
    # 要测内置技能的测试用 real_builtin_skills 夹具指回真实目录
    from heng_agent.tools import skill

    monkeypatch.setattr(skill, "BUILTIN_SKILLS", tmp_path_factory.mktemp("no-builtin-skills"))
    yield home


@pytest.fixture
def real_builtin_skills(monkeypatch: pytest.MonkeyPatch) -> Path:
    """用安装包里真实的内置技能（src/heng_agent/skills）。"""
    from heng_agent.tools import skill

    real = Path(skill.__file__).resolve().parent.parent / "skills"
    monkeypatch.setattr(skill, "BUILTIN_SKILLS", real)
    return real

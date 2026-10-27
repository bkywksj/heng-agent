"""git 工作树隔离：新建、列出、在里面改不碰主目录、删除（没合并的分支保留）、/worktree、-w。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent import cli, worktree
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.ui.repl import Repl


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "cart.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_create_list_and_main_stays_clean(repo: Path) -> None:
    created = worktree.create(repo, "fix-cart")
    assert created.path == (repo / ".heng-agent" / "worktrees" / "fix-cart").resolve()
    assert created.branch == "heng/fix-cart" and (created.path / "cart.py").is_file()
    assert git(repo, "status", "--porcelain") == ""  # 主仓库不把工作树当成一堆未跟踪文件
    assert worktree.repo_root(created.path) == repo.resolve()  # 在工作树里也能找到主仓库
    trees = worktree.list_worktrees(repo)
    assert [(w.main, w.branch) for w in trees] == [(True, "main"), (False, "heng/fix-cart")]
    assert worktree.create(repo, "fix-cart").path == created.path  # 同名再建：直接用已有的


def test_remove_keeps_unmerged_branch(repo: Path) -> None:
    wt = worktree.create(repo, "try").path
    (wt / "cart.py").write_text("x = 2\n", encoding="utf-8")
    git(wt, "commit", "-q", "-am", "试一下")
    note = worktree.remove(repo, "try")
    assert "还没合并" in note and "heng/try" in git(repo, "branch", "--list", "heng/try")  # 没合并：分支留着，活不丢
    worktree.create(repo, "empty")
    assert "与分支 heng/empty" in worktree.remove(repo, "empty")  # 没有新提交：分支一起删
    with pytest.raises(Exception, match="没有叫"):
        worktree.remove(repo, "nope")


def test_switch_session_into_worktree(repo: Path, tmp_path: Path) -> None:
    model = FakeModel([[("tool", "edit_file", {"path": "cart.py", "old_string": "x = 1", "new_string": "x = 99"})],
                       ["改好了"]])  # fmt: skip
    console = Console(record=True, width=160, force_terminal=False)
    agent = Agent(Settings(workdir=repo, home=tmp_path / "h", permission_mode="auto"), model)
    repl = Repl(agent, console=console)
    repl.handle_command("/worktree new 试改")
    wt = (repo / ".heng-agent" / "worktrees" / "试改").resolve()
    assert agent.settings.workdir == wt and agent.ctx.workdir == wt and str(wt) in agent.system
    agent.run("把 x 改成 99", lambda e: None, lambda t, a: "once")
    assert (wt / "cart.py").read_text(encoding="utf-8") == "x = 99\n"
    assert (repo / "cart.py").read_text(encoding="utf-8") == "x = 1\n"  # 主目录没被碰
    repl.handle_command("/worktree remove 试改")  # 正在里面：不让删
    repl.handle_command("/worktree")
    repl.handle_command("/worktree switch main")
    assert agent.settings.workdir == repo.resolve()
    assert [v for v, _, _ in repl._arg_hints("worktree", ["switch"])] == ["main", "试改"]
    shown = console.export_text()
    assert "已切到工作树 试改" in shown and "先 /worktree switch main 再删" in shown and "heng/试改" in shown


def test_not_a_repo(tmp_path: Path) -> None:
    console = Console(record=True, width=160, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([])), console=console)
    repl.handle_command("/worktree new x")
    assert "不是 git 仓库" in console.export_text()


def test_cli_flag(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HENG_AGENT_HOME", str(tmp_path / "home"))
    model = FakeModel([["好"]])
    seen: list[Path] = []
    real_agent = cli.Agent

    def capture(settings: Settings, *a: object, **kw: object) -> Agent:
        seen.append(settings.workdir)
        return real_agent(settings, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "Agent", capture)
    monkeypatch.setattr(cli, "make_client", lambda profile, max_tokens: model)
    assert cli.main(["--workdir", str(repo), "-w", "实验", "-p", "你好"]) == 0
    assert seen == [(repo / ".heng-agent" / "worktrees" / "实验").resolve()]  # 整个会话从工作树里开始

"""git worktree 隔离：在独立的工作目录（独立分支）里改代码，不碰主分支和你手上没提交的改动。

位置：仓库根目录下 `.heng-agent/worktrees/<名字>`，分支 `heng/<名字>`。那个目录放了一个只写着 `*` 的
.gitignore，主仓库不会把工作树里的东西当成一堆未跟踪文件。

合并不自动做：主目录得是干净的、还可能冲突，交给用户（或模型）在主目录里 `git merge heng/<名字>`。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from heng_agent.core.errors import AgentError

WORKTREE_DIR = Path(".heng-agent") / "worktrees"
BRANCH_PREFIX = "heng/"
_NAME = re.compile(r"[\w\-.]+")


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str  # 分离 HEAD 时为空
    main: bool  # 是不是主工作目录


def _git(args: list[str], cwd: Path) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentError(f"运行 git 失败：{exc}") from exc
    if done.returncode != 0:
        raise AgentError((done.stderr or done.stdout).strip() or f"git {' '.join(args)} 失败")
    return done.stdout


def repo_root(path: Path) -> Path:
    """path 所在仓库的主工作目录（在某个工作树里调用也返回主仓库的）。不是 git 仓库时报错。"""
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path).strip()
    return Path(common).parent.resolve()


def list_worktrees(root: Path) -> list[Worktree]:
    out, items, current = _git(["worktree", "list", "--porcelain"], root), [], {}
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            current = {"path": line[len("worktree ") :]}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].removeprefix("refs/heads/")
        elif not line and current:
            path = Path(current["path"]).resolve()
            items.append(Worktree(path, current.get("branch", ""), path == root.resolve()))
            current = {}
    return items


def path_of(root: Path, name: str) -> Path:
    return (root / WORKTREE_DIR / name).resolve()


def create(root: Path, name: str) -> Worktree:
    """新建工作树（分支 heng/名字，从当前 HEAD 拉出）；同名的已经在就直接返回它。"""
    if not _NAME.fullmatch(name):
        raise AgentError("名字只能用字母、数字、中文、_ - .")
    path = path_of(root, name)
    existing = next((w for w in list_worktrees(root) if w.path == path), None)
    if existing is not None:
        return existing
    folder = root / WORKTREE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")  # 主仓库不把工作树里的文件当成未跟踪文件
    branch = BRANCH_PREFIX + name
    branches = _git(["branch", "--list", branch], root).strip()
    if branches:  # 分支已经有了（上次删了工作树、留着分支）：接着用
        _git(["worktree", "add", str(path), branch], root)
    else:
        _git(["worktree", "add", "-b", branch, str(path), "HEAD"], root)
    return Worktree(path, branch, False)


def remove(root: Path, name: str, *, force: bool = False) -> str:
    """删工作树；分支没合并进当前分支时保留（免得丢活），返回说明。"""
    path = path_of(root, name)
    if not any(w.path == path for w in list_worktrees(root)):
        raise AgentError(f"没有叫 {name} 的工作树")
    _git(["worktree", "remove", *(["--force"] if force else []), str(path)], root)
    branch = BRANCH_PREFIX + name
    try:
        _git(["branch", "-d", branch], root)  # -d：没合并的分支 git 会拒绝删除
        return f"已删除工作树 {name} 与分支 {branch}"
    except AgentError:
        return f"已删除工作树 {name}；分支 {branch} 还没合并，先留着（合并后 git branch -d {branch}）"

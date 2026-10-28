"""路径安全：所有工具的文件路径都经这里解析，解析后必须仍在工作目录内。"""

from __future__ import annotations

from pathlib import Path

from heng_agent.core.errors import AgentError

#: 遍历目录时跳过：版本库、虚拟环境、依赖与缓存，内容大且几乎从不需要看
SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
     ".mypy_cache", "dist", "build", "target", ".idea", ".vscode"}
)  # fmt: skip


def resolve_in_workdir(raw: str, workdir: Path) -> Path:
    """把模型给的路径解析成绝对路径，越界（含 .. 与符号链接逃逸）就拒绝。

    先 resolve 再比较：只看字符串前缀挡不住 `a/../../x` 与指向外部的符号链接。
    """
    root = workdir.resolve()
    path = Path(raw)
    target = (path if path.is_absolute() else root / path).resolve()
    if target != root and root not in target.parents:
        raise AgentError(f"路径 {raw} 在工作目录 {root} 之外，拒绝访问")
    return target


def is_inside(path: Path, root: Path) -> bool:
    """path 解析后（跟随符号链接与 Windows 目录联接）是否仍在 root 内；root 要先 resolve 过。

    遍历目录的工具（grep、glob）对每个遍历到的条目都要问一遍：起点在工作目录里不代表遍历出的都在——
    工作目录里一个指向外部的链接就能把外面的文件带进来（以前 grep 就这样读出过工作目录外的文件内容）。
    """
    try:
        target = path.resolve()
    except OSError:  # 断掉的链接、没权限：当成在外面，不碰
        return False
    return target == root or root in target.parents


def display(path: Path, workdir: Path) -> str:
    """给模型和用户看的相对路径（正斜杠）。"""
    try:
        return path.resolve().relative_to(workdir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()

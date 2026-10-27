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


def display(path: Path, workdir: Path) -> str:
    """给模型和用户看的相对路径（正斜杠）。"""
    try:
        return path.resolve().relative_to(workdir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()

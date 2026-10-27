"""工具接口与注册表。新增工具 = 新建一个 Tool 子类 + 加进 builtin_tools()。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, ClassVar

from heng_agent.core.types import ToolResult
from heng_agent.tools.background import TaskManager


@dataclass
class ToolContext:
    """工具执行时能拿到的环境。

    - workdir：读写边界；bash_timeout：命令超时；
    - snapshot：改文件前调用，交给智能体记下原内容，/undo 时恢复（不设就不记）；
    - todos：任务清单的当前状态（todo 工具读写，界面显示）。
    """

    workdir: Path
    bash_timeout: float = 120.0
    snapshot: Callable[[Path], None] | None = None
    todos: list[dict[str, str]] = field(default_factory=list)
    #: 后台任务（bash run_in_background、Ctrl+B 转过来的命令）
    tasks: TaskManager = field(default_factory=TaskManager)
    #: 用户按了 Ctrl+B：正在前台跑的命令转到后台
    detach: Event = field(default_factory=Event)

    def before_write(self, path: Path) -> None:
        if self.snapshot is not None:
            self.snapshot(path)


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[dict[str, Any]]
    #: "read"：只读，直接执行；"edit"：改文件；"exec"：跑命令；"net"：访问网络（可能把内容带出去）。
    #: 权限按这个分类决定是否要确认（permissions.RULES）
    kind: ClassVar[str] = "read"

    def spec(self) -> dict[str, Any]:
        """发给模型的工具声明（Anthropic tools 格式）。"""
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def summary(self, args: dict[str, Any]) -> str:
        """给用户看的一行摘要（确认对话与执行日志用）。"""
        return ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())

    @abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


def builtin_tools() -> list[Tool]:
    from heng_agent.tools.files import EditFile, ReadFile, WriteFile
    from heng_agent.tools.search import Glob, Grep
    from heng_agent.tools.shell import Bash, BashOutput, KillTask
    from heng_agent.tools.todo import Todo
    from heng_agent.tools.web import WebFetch

    return [ReadFile(), Glob(), Grep(), WriteFile(), EditFile(), Bash(), BashOutput(), KillTask(), Todo(), WebFetch()]
